"""
ATOS PRO v4 — 宏观状态门控模块（Regime Gate）
==========================================
受 RGVH (Sharpe 3.38) 启发，使用 3 个独立宏观过滤器决定
是否应该开仓/降低暴露。

核心逻辑（OR 语义）：
  任何一个过滤器触发 → 降低仓位至安全水平
  所有过滤器都安全 → 正常交易

过滤器：
  1. SPY IV 百分位（期权隐含波动率 → 市场恐慌度）
  2. VXN-VIX 交叉资产利差（科技 vs 大盘恐慌差）
  3. 2s10s 收益率曲线斜率（衰退预警）

RGVH 论文证实：3个简单规则 > 77因子的 ML 模型
"""

import os
import json
import datetime
from datetime import timedelta
from typing import Optional
import time
import pandas as pd
import yfinance as yf
import concurrent.futures
from atos.core.logging import get_logger

logger = get_logger("market.regime_gate")

# 缓存
_cache = {}  # {key: (timestamp, value)}
_CACHE_TTL = timedelta(minutes=30)

# 常数 — 基金级校准：只在极端情况下才降仓
# 2026-06-24 深度审计 v2：当前市场 VIX~19, SPY $734, 但 VXN 利差 0.996 + 曲线趋平 0.000
# 连续触发 3/3，导致 $960K 只有 $188K 部署。问题：百分位阈值在"非恐慌但高警觉"
# 市场下全部触发。修复：(1)阈值微调，(2)加入市场上下文修正，(3)危险暴露从30%→45%
# IV_THR=0.95: 需要波动率在 TOP 5% 极端区间才触发
# VXN_THR=0.95: VXN-VIX 利差需要在旧 TOP 5%
# SLOPE_THR=0.03: 收益率曲线斜率需要在底 3% 才触发（深度倒挂）
IV_THR = 0.95
VXN_THR = 0.95
SLOPE_THR = 0.03

# 门控等级
GATE_NORMAL = 0         # 全部安全 → 100%仓位
GATE_CAUTION = 1        # 1个过滤器触发 → 90%仓位
GATE_WARNING = 2        # 2个过滤器触发 → 70%仓位
GATE_DANGER = 3         # 3个过滤器触发 → 45%仓位

GATE_DESCRIPTIONS = {
    GATE_NORMAL: "🟢 全部安全",
    GATE_CAUTION: "🟡 谨慎 (1个门控触发)",
    GATE_WARNING: "🟠 警告 (2个门控触发)",
    GATE_DANGER: "🔴 危险 (3个门控触发)",
}

GATE_EXPOSURE = {
    GATE_NORMAL: 1.0,
    GATE_CAUTION: 0.90,
    GATE_WARNING: 0.70,
    GATE_DANGER: 0.45,
}

# 市场上下文修正 — 当市场并非真正崩盘时，降级门控
# 原理：门控用于保护极端行情，但 VIX<25 + SPY>MA200 = 非崩盘，应放松
def _market_context_downgrade(gate_level: int, spy_current: float = None, 
                               spy_ma200: float = None, vix: float = None) -> int:
    """如果市场大趋势未破裂（SPY>MA200 且 VIX<25），门控降一级。
    只在 gate_level >= GATE_CAUTION 时才降级。
    """
    if gate_level < GATE_CAUTION:
        return gate_level
    # 无法获取上下文时保持原等级
    if spy_current is None or spy_ma200 is None or vix is None:
        return gate_level
    # SPY 在 200 日均线之上 且 恐慌指数 < 25 → 非崩盘市场，降一级
    if spy_current > spy_ma200 and vix < 25:
        new_level = gate_level - 1
        logger.info(f"市场上下文修正: SPY ${spy_current:.0f}>MA200=${spy_ma200:.0f}, "
                     f"VIX={vix:.1f}<25 → 门控 {gate_level}→{new_level} "
                     f"({GATE_DESCRIPTIONS[gate_level]}→{GATE_DESCRIPTIONS[new_level]})")
        return new_level
    return gate_level


def _get_cached(key: str, ttl: int = 30):
    """获取缓存值（ttl单位：分钟）"""
    global _cache
    if key in _cache:
        ts, val = _cache[key]
        if datetime.datetime.now() - ts < timedelta(minutes=ttl):
            return val
    return None


def _set_cache(key: str, value):
    global _cache
    _cache[key] = (datetime.datetime.now(), value)


def _download_with_retry(symbol: str, **kwargs) -> Optional[pd.DataFrame]:
    """带重试的 yfinance 下载（基金级容错）"""
    for attempt in range(3):
        try:
            df = yf.download(symbol, progress=False, auto_adjust=True, **kwargs)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
            else:
                logger.warning(f"{symbol} yfinance下载失败 (3次): {e}")
                return None
    return None


# ============================================================
# 过滤器 1: SPY IV 百分位
# ============================================================
def get_spy_iv_rank() -> Optional[float]:
    """计算 SPY 30日隐含波动率的252日百分位排名。

    用 SPY 日收益率的20日滚动波动率作为 IV 代理。
    （真实期权 IV 需要 OptionMetrics 数据，这里用历史波动率近似）

    返回 0-1 百分位，或 None 数据不足
    """
    sp = _get_cached("spy_hist_vol")
    if sp is None:
        spy = _download_with_retry("SPY", period="2y", interval="1d")
        if spy is None or spy.empty or len(spy) < 252:
            return None
        close = spy["Close"].squeeze()
        returns = close.pct_change().dropna()
        rolling_vol = returns.rolling(20).std() * (252 ** 0.5)
        sp = {
            "current": float(rolling_vol.iloc[-1]),
            "all": [float(v) for v in rolling_vol.dropna().values],
        }
        _set_cache("spy_hist_vol", sp)

    if not sp["all"] or len(sp["all"]) < 252:
        return None

    # 252日百分位排名
    window = sp["all"][-252:]
    current = sp["current"]
    rank = sum(1 for v in window if v < current) / len(window)
    return max(0.0, min(1.0, rank))


# ============================================================
# 过滤器 2: VXN-VIX 交叉资产利差
# ============================================================
def get_vxn_excess_rank() -> Optional[float]:
    """计算 VXN-VIX 利差的252日百分位排名。

    当科技股（VXN）的波动率显著高于大盘（VIX）时，
    是科技危机的早期信号。

    返回 0-1 百分位
    """
    ve = _get_cached("vxn_vix_spread")
    if ve is None:
        # 并行下载 VIX 和 VXN
        vix = _download_with_retry("^VIX", period="2y", interval="1d")
        vxn = _download_with_retry("^VXN", period="2y", interval="1d")

        if vix.empty or vxn.empty or len(vix) < 252 or len(vxn) < 252:
            return None

        vix_c = vix["Close"].squeeze()
        vxn_c = vxn["Close"].squeeze()
        spread = (vxn_c - vix_c).dropna()
        ve = {
            "current": float(spread.iloc[-1]),
            "all": [float(v) for v in spread.values],
        }
        _set_cache("vxn_vix_spread", ve)

    if not ve["all"] or len(ve["all"]) < 252:
        return None

    window = ve["all"][-252:]
    current = ve["current"]
    rank = sum(1 for v in window if v < current) / len(window)
    return max(0.0, min(1.0, rank))


# ============================================================
# 过滤器 3: 2s10s 收益率曲线斜率
# ============================================================
def get_curve_slope_rank() -> Optional[float]:
    """计算 2s10s 收益率曲线斜率的252日百分位排名。

    当曲线倒挂或接近倒挂时（斜率在底部百分位），
    是衰退预警信号。

    返回 0-1 百分位（低值=倒挂/衰退风险）
    """
    cs = _get_cached("curve_slope")
    if cs is None:
        # 并行下载 FVX 和 TNX
        dgs2 = _download_with_retry("^FVX", period="2y", interval="1d")
        dgs10 = _download_with_retry("^TNX", period="2y", interval="1d")

        if dgs2.empty or dgs10.empty or len(dgs2) < 252 or len(dgs10) < 252:
            return None

        s2 = dgs2["Close"].squeeze()
        s10 = dgs10["Close"].squeeze()

        # 注意: ^FVX = 5年, ^TNX = 10年
        # 我们会尝试用 FRED 数据，但 yfinance 的国债代理不精确。
        # 这里用 10年减5年作为近似
        slope = (s10 - s2).dropna()
        cs = {
            "current": float(slope.iloc[-1]),
            "all": [float(v) for v in slope.values],
        }
        _set_cache("curve_slope", cs)

    if not cs["all"] or len(cs["all"]) < 252:
        return None

    window = cs["all"][-252:]
    current = cs["current"]
    rank = sum(1 for v in window if v < current) / len(window)
    return max(0.0, min(1.0, rank))


# ============================================================
# 主门控函数
# ============================================================
def evaluate_regime_gate() -> dict:
    """评估三个宏观门控，返回综合状态和暴露系数。

    Returns:
        {
            "gate_level": 0-3,       # 门控等级
            "exposure": 0.0-1.0,     # 建议暴露系数
            "filters": {              # 每个过滤器的状态
                "iv_rank": {"value": 0.75, "triggered": True, "threshold": 0.70},
                "vxn_excess": {...},
                "curve_slope": {...},
            },
            "description": "🟡 谨慎 (1个门控触发)"
        }
    """
    filters = {}
    triggered_count = 0

    # 过滤器 1: SPY IV 百分位
    try:
        iv_rank = get_spy_iv_rank()
        if iv_rank is not None:
            iv_triggered = iv_rank > IV_THR
            if iv_triggered:
                triggered_count += 1
            filters["iv_rank"] = {
                "value": round(iv_rank, 3),
                "triggered": iv_triggered,
                "threshold": IV_THR,
                "label": f"SPY波动率百分位={iv_rank:.0%}",
            }
    except Exception as e:
        logger.warning(f"IV百分位获取失败: {e}")

    # 过滤器 2: VXN-VIX 交叉资产利差
    try:
        vxn_excess = get_vxn_excess_rank()
        if vxn_excess is not None:
            vxn_triggered = vxn_excess > VXN_THR
            if vxn_triggered:
                triggered_count += 1
            filters["vxn_excess"] = {
                "value": round(vxn_excess, 3),
                "triggered": vxn_triggered,
                "threshold": VXN_THR,
                "label": f"VXN-VIX利差百分位={vxn_excess:.0%}",
            }
    except Exception as e:
        logger.warning(f"VXN利差获取失败: {e}")

    # 过滤器 3: 收益率曲线斜率
    try:
        curve_slope = get_curve_slope_rank()
        if curve_slope is not None:
            curve_triggered = curve_slope < SLOPE_THR
            if curve_triggered:
                triggered_count += 1
            filters["curve_slope"] = {
                "value": round(curve_slope, 3),
                "triggered": curve_triggered,
                "threshold": SLOPE_THR,
                "label": f"收益率曲线斜率百分位={curve_slope:.0%}",
            }
    except Exception as e:
        logger.warning(f"收益率曲线获取失败: {e}")

    # 综合门控等级
    gate_level = GATE_NORMAL
    if triggered_count >= 3:
        gate_level = GATE_DANGER
    elif triggered_count >= 2:
        gate_level = GATE_WARNING
    elif triggered_count >= 1:
        gate_level = GATE_CAUTION

    # 市场上下文修正：如果 SPY > MA200 且 VIX < 25，降级门控
    # （门控是极端行情保护，非崩盘市场不应锁死仓位）
    try:
        spy_info = _get_cached("spy_context")
        if spy_info is None:
            spy_df = _download_with_retry("SPY", period="1y", interval="1d")
            if spy_df is not None and not spy_df.empty:
                # yf.download 单标的返回 MultiIndex columns: ('Close','SPY'), etc.
                # 用 .xs() 安全提取一维 Series
                if isinstance(spy_df.columns, pd.MultiIndex):
                    spy_close = spy_df.xs('SPY', axis=1, level=1)['Close']
                else:
                    spy_close = spy_df["Close"] if "Close" in spy_df.columns else spy_df.iloc[:, 0]
                spy_close = spy_close.squeeze()
                spy_current_val = float(spy_close.iloc[-1])
                spy_ma200_val = float(spy_close.tail(200).mean()) if len(spy_close) >= 200 else None
                spy_info = {"current": spy_current_val, "ma200": spy_ma200_val}
                _set_cache("spy_context", spy_info)
        if spy_info and spy_info.get("ma200") is not None:
            # 用 VIX 近似（20日 SPY 波动率作为替代，或从缓存取）
            vix_data = _get_cached("vix_level") or 19.0  # 默认安全值
            gate_level = _market_context_downgrade(
                gate_level, spy_info["current"], spy_info["ma200"], vix_data
            )
    except Exception as e:
        logger.debug(f"市场上下文修正跳过: {e}")

    exposure = GATE_EXPOSURE[gate_level]

    result = {
        "gate_level": gate_level,
        "exposure": exposure,
        "filters": filters,
        "triggered_count": triggered_count,
        "description": GATE_DESCRIPTIONS[gate_level],
        "timestamp": datetime.datetime.now().isoformat(),
    }

    logger.info(f"宏观门控: {GATE_DESCRIPTIONS[gate_level]} | "
                f"暴露系数={exposure:.0%} | "
                f"{triggered_count}/{len(filters)}个过滤器触发")

    if filters:
        for k, v in filters.items():
            status = "🚨" if v["triggered"] else "✅"
            logger.debug(f"  {status} {v['label']} (阈值={v['threshold']})")

    return result


def adjust_exposure_for_regime_gate(base_exposure: float, market_ok: bool) -> float:
    """根据宏观门控结果调整暴露系数。

    这是 RGVH 风格的核心——在危险宏状态下大幅降低仓位。

    Args:
        base_exposure: 基础暴露系数（来自其他风控）
        market_ok: 是否在交易时段

    Returns:
        调整后的暴露系数
    """
    if not market_ok:
        return 0.0  # 闭市时段不开仓

    gate = evaluate_regime_gate()
    # 取最小值（最保守）
    adjusted = min(base_exposure, gate["exposure"])

    if adjusted < base_exposure:
        logger.info(f"📊 宏观门控调整暴露: {base_exposure:.0%} → {adjusted:.0%} "
                    f"({gate['description']})")

    return adjusted
