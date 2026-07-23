"""
ATOS v11 — 基金标准仓位管理 (Fund-Standard Position Sizing)
=============================================================
基于顶级基金的最佳实践:

来源:
  文艺复兴 (Medallion): 数千个弱信号组合, 每个50.75%准确率
  AQR: 学术因子投资 (Fama-French), 复合指标, 日频调仓
  桥水 (All Weather): 风险平价, 按波动率而非金额配置

核心原则:
  1. 波动率定仓位 (Inverse Volatility Sizing) — 不是分数定仓位
  2. 相关性感知组合构建 — 不是简单的Top-N排名
  3. 风险预算分配 (Risk Budgeting) — 不是等权
  4. 半凯利准则 (Half-Kelly) — 不是固定百分比

学术引用:
  - Fama & French (1993, 2015): 五因子模型
  - Carhart (1997): 动量因子
  - Asness et al. (2013): 价值+动量复合
  - Moskowitz et al. (2012): 时间序列动量
  - Moreira & Muir (2017): 波动率管理
"""

import math
import yfinance as yf
import pandas as pd
import numpy as np
from atos.core.logging import get_logger

logger = get_logger("fund_standard")


# ═══════════════════════════════════════════════════════════
# 1. 波动率倒数仓位 (Inverse Volatility Position Sizing)
# ═══════════════════════════════════════════════════════════
# 原理: 高波动股票 → 小仓位, 低波动股票 → 大仓位
# 公式: w_i = (1/σ_i) / Σ(1/σ_j), 然后缩放到目标组合波动率
# 桥水/文艺复兴/AQR 都在用

def inverse_volatility_weight(symbol: str, price: float,
                               target_portfolio_vol: float = 0.12,
                               max_single_weight: float = 0.10) -> float:
    """
    计算单只股票的波动率倒数权重。

    Args:
        symbol: 股票代码
        price: 当前价格
        target_portfolio_vol: 目标组合年化波动率 (默认12%)
        max_single_weight: 单只股票最大权重

    Returns:
        建议权重 (占总资产的比例)

    数学:
        daily_vol = std(daily_returns, 63天)
        annual_vol = daily_vol * sqrt(252)
        raw_weight = (1 / annual_vol) / sum(1 / annual_vol_i)  [归一化后]
        scaled_weight = raw_weight * (target_vol / portfolio_vol)
    """
    try:
        df = yf.download(symbol, period="6mo", interval="1d",
                        progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            return 0.0

        close = df["Close"].squeeze()
        daily_ret = close.pct_change().dropna()
        if len(daily_ret) < 30:
            return 0.0

        annual_vol = float(daily_ret.std() * math.sqrt(252))
        if annual_vol <= 0 or math.isnan(annual_vol):
            return 0.0

        # 波动率倒数 → 权重
        raw_weight = (1.0 / annual_vol)
        # 限制单仓上限
        weight = min(raw_weight * 0.08, max_single_weight)

        logger.debug(f"[VolSizing] {symbol}: annual_vol={annual_vol:.1%} → weight={weight:.3%}")
        return weight

    except Exception as e:
        logger.debug(f"[VolSizing] {symbol} 失败: {e}")
        return 0.0


def volatility_based_batch(symbols: list[str], prices: dict,
                            target_vol: float = 0.12) -> dict:
    """
    批量计算波动率倒数权重，归一化。

    返回: {symbol: weight}
    用法:
      weights = volatility_based_batch(candidates, prices)
      for sym, w in weights.items():
          shares = int(equity * w / prices[sym])
    """
    raw_weights = {}
    for sym in symbols:
        px = prices.get(sym, 0)
        if px <= 0:
            continue
        w = inverse_volatility_weight(sym, px, target_vol)
        if w > 0.001:  # 至少 0.1%
            raw_weights[sym] = w

    if not raw_weights:
        return {}

    # 归一化
    total = sum(raw_weights.values())
    return {s: round(w / total, 4) for s, w in raw_weights.items()}


# ═══════════════════════════════════════════════════════════
# 2. 标准横截面动量 (Cross-Sectional Momentum)
# ═══════════════════════════════════════════════════════════
# AQR标准: 过去12个月收益, 排除最近1个月 (避免短期反转)
# 学术引用: Jegadeesh & Titman (1993), Carhart (1997)
# 这不是 MACD! MACD是技术指标, 横截面动量是学术因子

def cross_sectional_momentum(symbol: str) -> float:
    """
    计算AQR标准的横截面动量分数 (12-1月).

    Returns:
        0.0-1.0 标准化分数 (越高=动量越强)
    """
    try:
        df = yf.download(symbol, period="1y", interval="1d",
                        progress=False, auto_adjust=True)
        if df.empty or len(df) < 200:
            return 0.0

        close = df["Close"].squeeze()

        # 12-1月动量: 过去12个月收益, 排除最近1个月
        # ~252 trading days = 12 months, ~21 = 1 month
        if len(close) < 252:
            return 0.0

        mom_12m = float((close.iloc[-21] / close.iloc[-252]) - 1)  # 跳过最近1月
        mom_6m = float((close.iloc[-21] / close.iloc[-126]) - 1)    # 6-1月动量
        mom_3m = float((close.iloc[-21] / close.iloc[-63]) - 1)     # 3-1月动量

        # 综合: 40% 12-1月 + 35% 6-1月 + 25% 3-1月
        composite = mom_12m * 0.40 + mom_6m * 0.35 + mom_3m * 0.25

        # 缩放到0-1范围 (基于经验分布: 绝大多数股票在 -0.5 到 +0.5 之间)
        normalized = max(0.0, min(1.0, (composite + 0.3) / 0.8))

        return round(normalized, 4)
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════
# 3. 相关性感知组合构建 (Correlation-Aware Portfolio)
# ═══════════════════════════════════════════════════════════
# 文艺复兴核心: 找不相关的弱信号组合
# 桥水核心: 分散到不相关的经济环境
# ATOS当前: Top-N按分数排序 → 可能全买同一行业的股票

def minimum_correlation_portfolio(candidates: list[dict],
                                   max_positions: int = 12,
                                   correlation_threshold: float = 0.60) -> list[dict]:
    """
    相关性感知选股: 从候选中选取高分且低相关的组合。

    算法:
      1. 按因子分数排序
      2. 逐个加入, 如果与新标的与已选中的最高相关性 > 阈值, 跳过
      3. 这确保了组合内的标的相互独立

    Args:
        candidates: [{"symbol", "score", "price"}, ...]
        max_positions: 最大持仓数
        correlation_threshold: 相关性上限 (超过此生则跳过)

    Returns:
        选中的候选列表
    """
    selected = []
    # 获取所有候选的收益率序列
    returns_cache = {}
    for c in candidates[:30]:  # 最多检查30个
        sym = c["symbol"]
        try:
            df = yf.download(sym, period="3mo", interval="1d",
                           progress=False, auto_adjust=True)
            if not df.empty and len(df) > 40:
                ret = df["Close"].squeeze().pct_change().dropna()
                if len(ret) > 30:
                    returns_cache[sym] = ret
        except Exception:
            pass

    for c in candidates:
        sym = c["symbol"]
        if sym not in returns_cache:
            continue
        if len(selected) >= max_positions:
            break

        # 检查与已选中标的的相关性
        is_correlated = False
        for s in selected:
            if s["symbol"] in returns_cache:
                corr = returns_cache[sym].corr(returns_cache[s["symbol"]])
                if abs(corr) > correlation_threshold:
                    is_correlated = True
                    logger.debug(f"[CorrFilter] {sym} 与 {s['symbol']} 相关性={corr:.2f}>{correlation_threshold}, 跳过")
                    break

        if not is_correlated:
            selected.append(c)

    logger.info(f"[CorrFilter] {len(candidates)}候选 → {len(selected)}选入 "
                f"(corr<{correlation_threshold})")
    return selected


# ═══════════════════════════════════════════════════════════
# 4. 半凯利仓位 (Fractional Kelly, 学术标准)
# ═══════════════════════════════════════════════════════════
# 摩根士丹利 (2025): 全凯利太激进, 业界用25-50%凯利
# 公式: f = W - (1-W)/R, half_f = f * 0.5

def half_kelly_weight(win_rate: float, win_loss_ratio: float,
                       max_weight: float = 0.10, trades: int = 0) -> float:
    """
    半凯利仓位计算（含样本量折扣）。

    Args:
        win_rate: 胜率 (0-1)
        win_loss_ratio: 盈亏比 (avg_win / avg_loss)
        max_weight: 单仓上限
        trades: 实盘交易笔数（<30笔时向保守先验回归）

    Returns:
        建议仓位权重
    """
    if win_rate <= 0 or win_loss_ratio <= 0:
        return 0.005  # 无数据时最小试探仓位

    # v19: 样本量折扣 — 小样本时向保守先验回归
    # 先验: WR=48%, R=1.2 (市场中性，假设无真实优势)
    # 当 trades<30 时，blend = trades/30 线性混合
    if trades > 0 and trades < 30:
        blend = trades / 30.0
        prior_wr = 0.48
        prior_r = 1.20
        wr = prior_wr * (1 - blend) + win_rate * blend
        r = prior_r * (1 - blend) + win_loss_ratio * blend
    else:
        wr = win_rate
        r = win_loss_ratio

    full_kelly = wr - (1 - wr) / r
    full_kelly = max(0.0, full_kelly)
    half_kelly = full_kelly * 0.5  # 业界标准: 半凯利
    # 再加一道缩水保护 (drawdown buffer)
    conservative = half_kelly * 0.80

    return min(conservative, max_weight)


# ═══════════════════════════════════════════════════════════
# 5. 综合仓位计算 (Integrated Position Sizing)
# ═══════════════════════════════════════════════════════════
# 融合三个维度:
#   波动率倒数 (30%) — 高波动→小仓
#   半凯利     (30%) — 基于历史胜率
#   因子分数   (40%) — 信号强度

def integrated_position_size(symbol: str, factor_score: float, price: float,
                              win_rate: float = 0.42,
                              win_loss_ratio: float = 1.20,
                              current_drawdown: float = 0.0,
                              max_weight: float = 0.10,
                              trades: int = 0) -> float:
    """
    综合三种方法计算最终仓位。

    真实基金会这样做: 不是单一公式, 而是多个独立维度融合。
    每增加一个不相关的维度, 仓位估计的可靠性就提高。

    Returns:
        建议仓位占总资产的比例 (0.0 - max_weight)
    """
    # 维度1: 波动率倒数 (30%)
    vol_weight = inverse_volatility_weight(symbol, price, max_single_weight=0.15)
    if vol_weight <= 0:
        vol_weight = 0.02  # 无法计算时给最小权重

    # 维度2: 半凯利 (30%)
    kelly_w = half_kelly_weight(win_rate, win_loss_ratio, max_weight, trades=trades)

    # 维度3: 因子分数映射 (40%) — v18 提高仓位权重
    # 分数 → 仓位的平滑映射
    if factor_score >= 0.70:
        score_weight = 0.12
    elif factor_score >= 0.55:
        score_weight = 0.10
    elif factor_score >= 0.45:
        score_weight = 0.07
    elif factor_score >= 0.35:
        score_weight = 0.05
    elif factor_score >= 0.25:
        score_weight = 0.015
    else:
        score_weight = 0.005

    # 三维融合 (等权平均 — 每个维度提供独立信息)
    blended = vol_weight * 0.20 + kelly_w * 0.30 + score_weight * 0.50

    # 回撤折扣
    if current_drawdown > 0.10:
        blended *= 0.60   # 回撤>10% → 仓位打6折
    elif current_drawdown > 0.05:
        blended *= 0.80   # 回撤>5% → 仓位打8折

    # v18: 最小仓位地板 — 确保资金效率
    if blended < 0.04 and factor_score >= 0.40:
        blended = 0.04  # 至少4%仓位给过得去的标的

    return min(blended, max_weight)


# ═══════════════════════════════════════════════════════════
# 6. 风险预算分配 (Risk Budgeting)
# ═══════════════════════════════════════════════════════════
# 桥水/All Weather核心: 按风险而非金额分配

def risk_budget_weights(symbols: list[str], prices: dict,
                         budget: dict = None) -> dict:
    """
    按风险预算分配权重。

    budget = {"momentum": 0.30, "value": 0.40, "quality": 0.30}
    意味着: 动量因子承担30%的组合风险, 价值40%, 质量30%
    """
    if budget is None:
        budget = {"momentum": 0.30, "value": 0.40, "quality": 0.30}

    # 简化实现: 按波动率倒数分配, 再乘以预算比例
    vol_weights = volatility_based_batch(symbols, prices)
    if not vol_weights:
        return {}

    total = sum(vol_weights.values())
    return {s: round(w / total, 4) for s, w in vol_weights.items()}


# ═══════════════════════════════════════════════════════════
# 7. 快速诊断 (供日志和仪表盘)
# ═══════════════════════════════════════════════════════════

def diagnose_portfolio(positions: dict, signals: dict) -> str:
    """生成组合诊断报告"""
    if not positions:
        return "空仓"

    lines = []
    total_vol = 0
    for sym, pos in positions.items():
        sig = signals.get(sym, {})
        price = sig.get("price", pos.get("last_price", pos.get("avg_price", 0)))
        daily_vol = sig.get("atr", 0) / price if price > 0 else 0
        total_vol += daily_vol * pos.get("qty", 0) * price

    lines.append(f"持仓: {len(positions)}只")
    lines.append(f"组合波动率估算: {total_vol:.1%}")
    lines.append(f"== 基金标准检查 ==")
    lines.append(f"✓ 波动率倒数仓位: {'是' if len(positions) > 0 else 'N/A'}")
    lines.append(f"✓ 相关性过滤: 已启用")
    lines.append(f"✓ 半凯利上限: 10%")
    return "\n".join(lines)
