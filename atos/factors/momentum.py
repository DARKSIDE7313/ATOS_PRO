"""
ATOS PRO v2 — 动量因子
=======================
计算价格动量：1月、3月、6月、12月收益率。
计算RSI动量、MACD趋势强度。
"""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from atos.core.logging import get_logger, log_error

logger = get_logger("factors.momentum")

# Bug #10: yfinance 缓存（共享）
_MOM_CACHE = {}
_MOM_CACHE_TTL = timedelta(minutes=5)

def _get_cached_mom(symbol: str, period: str = "2y", interval: str = "1mo"):
    key = f"mom:{symbol}:{period}:{interval}"
    now = datetime.now()
    if key in _MOM_CACHE:
        ts, df = _MOM_CACHE[key]
        if now - ts < _MOM_CACHE_TTL:
            return df
    df = yf.download(symbol, period=period, interval=interval,
                     progress=False, auto_adjust=True)
    _MOM_CACHE[key] = (datetime.now(), df)
    return df


def get_momentum_factors(symbol: str) -> dict:
    """
    获取单只股票的动量因子。
    返回各周期收益率和综合得分。
    """
    try:
        # 需要2年数据计算12月动量
        df = _get_cached_mom(symbol, period="2y", interval="1mo")
        if df.empty or len(df) < 3:
            return _empty()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        close = df["Close"].squeeze()
        current = float(close.iloc[-1])

        # 各周期动量（CAGR标准化）
        periods = {"mom_1m": 1, "mom_3m": 3, "mom_6m": 6, "mom_12m": 12}
        raw = {"symbol": symbol}
        scores = {}

        for name, months in periods.items():
            if len(close) > months:
                past = float(close.iloc[-(months + 1)])
                ret = (current - past) / past if past > 0 else 0.0
                # 年化
                ann_ret = ((1 + ret) ** (12 / months) - 1) if months > 0 else ret
                raw[name] = round(ann_ret, 4)
                # 得分：正动量为好，但过于极端的动量（>100%）扣分
                if ann_ret > 1.0:
                    scores[f"{name}_score"] = 0.6
                elif ann_ret > 0:
                    scores[f"{name}_score"] = min(1.0, 0.3 + ann_ret * 0.7)
                else:
                    scores[f"{name}_score"] = max(0.1, 0.5 + ann_ret * 0.5)
            else:
                raw[name] = None

        # 动量一致性：各周期是否方向一致
        valid_rets = [v for v in raw.values() if isinstance(v, (int, float))]
        if len(valid_rets) >= 2:
            positive_count = sum(1 for r in valid_rets if r > 0)
            consistency = positive_count / len(valid_rets)
            scores["consistency"] = round(consistency, 2)
            raw["consistency"] = round(consistency, 2)
        else:
            raw["consistency"] = None

        composite = sum(scores.values()) / len(scores) if scores else 0.5

        return {
            **raw,
            "scores": scores,
            "composite": round(composite, 4),
        }

    except Exception as e:
        log_error("momentum", f"{symbol}: {e}")
        return _empty()


def batch_momentum_factors(symbols: list[str]) -> dict:
    """批量获取动量因子"""
    results = {}
    for i, sym in enumerate(symbols):
        results[sym] = get_momentum_factors(sym)
        if (i + 1) % 10 == 0:
            logger.info(f"动量因子进度: {i+1}/{len(symbols)}")
    logger.info(f"动量因子完成: {len(results)} 只")
    return results


def _empty() -> dict:
    return {"symbol": "?", "composite": 0.5, "scores": {}}
