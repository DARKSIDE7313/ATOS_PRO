"""
ATOS Intel — 交易前情报简报生成器
================================
在 AI 做出交易决策前，自动生成综合情报简报。
这是 AI 决策的"眼睛和耳朵"。

简报包含:
  1. 市场情绪摘要
  2. 重要新闻头条 (高影响)
  3. 内部人交易信号
  4. 经济日历事件
  5. 行业板块动态
  6. 关键标的新闻

用法:
  from atos.intel.briefing import get_pre_trade_briefing
  briefing = get_pre_trade_briefing(symbols=["AAPL","NVDA","SPY"])
  # briefing 是一个结构化的 dict，直接喂给 AI 引擎
"""

import time, json, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional
from atos.core.logging import get_logger
from atos.intel.news_engine import (
    fetch_market_news, fetch_stock_news, fetch_insider_trades,
    fetch_finnhub_news
)
from atos.intel.sentiment import get_sentiment_summary

logger = get_logger("intel.briefing")


def get_pre_trade_briefing(
    symbols: List[str] = None,
    include_news: bool = True,
    include_sentiment: bool = True,
    include_insider: bool = True,
    max_news: int = 15,
) -> dict:
    """
    生成交易前情报简报。

    Args:
        symbols: 关注的股票列表（默认: SPY, QQQ, AAPL, NVDA, MSFT）
        include_news: 包含新闻
        include_sentiment: 包含情绪分析
        include_insider: 包含内部人交易
        max_news: 最多返回多少条新闻

    Returns:
        {
            "timestamp": "2026-07-14T09:30:00",
            "market_sentiment": {...},
            "top_news": [...],
            "insider_signals": [...],
            "watchlist_news": {...},
            "trading_implications": [...],
            "risk_flags": [...],
        }
    """
    if symbols is None:
        symbols = ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA", "META", "GOOGL"]

    start_time = time.time()
    briefing = {
        "timestamp": dt.datetime.now().isoformat(),
        "generated_in_seconds": 0,
        "market_sentiment": {},
        "top_news": [],
        "insider_signals": [],
        "watchlist_news": {},
        "trading_implications": [],
        "risk_flags": [],
    }

    # 并行获取数据
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}

        if include_sentiment:
            futures[executor.submit(get_sentiment_summary)] = "sentiment"

        if include_news:
            futures[executor.submit(fetch_market_news, max_news)] = "market_news"
            futures[executor.submit(fetch_stock_news, symbols)] = "stock_news"

        if include_insider:
            futures[executor.submit(fetch_insider_trades, None, 10)] = "insider"

        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
                if key == "sentiment":
                    briefing["market_sentiment"] = result
                elif key == "market_news":
                    briefing["top_news"] = result[:max_news]
                elif key == "stock_news":
                    briefing["watchlist_news"] = result
                elif key == "insider":
                    briefing["insider_signals"] = result
            except Exception as e:
                logger.debug(f"简报数据源 {key}: {e}")

    # 生成交易建议
    briefing["trading_implications"] = _generate_implications(briefing)
    briefing["risk_flags"] = _detect_risk_flags(briefing)
    briefing["generated_in_seconds"] = round(time.time() - start_time, 2)

    return briefing


def _generate_implications(briefing: dict) -> List[str]:
    """根据情报生成交易建议"""
    implications = []

    # 情绪分析
    sentiment = briefing.get("market_sentiment", {})
    bias = sentiment.get("bias", "NEUTRAL")
    fg = sentiment.get("fear_greed", {})

    if bias == "BULLISH":
        implications.append("✅ 市场情绪乐观 → 可适度加大仓位")
    elif bias == "BEARISH":
        implications.append("⚠️ 市场情绪悲观 → 减仓或对冲")
    else:
        implications.append("➡️ 市场情绪中性 → 维持标准仓位")

    if fg.get("zone") == "EXTREME_GREED":
        implications.append("🔴 极度贪婪区域 → 注意回调风险，避免追高")
    elif fg.get("zone") == "EXTREME_FEAR":
        implications.append("🟢 极度恐惧区域 → 可能是抄底机会（分批入场）")

    vix = sentiment.get("vix", {})
    vix_val = vix.get("vix", 18)
    if vix_val > 25:
        implications.append(f"⚠️ VIX={vix_val:.0f} 高波动 → 收紧止损，降低单仓上限")
    elif vix_val < 13:
        implications.append(f"💤 VIX={vix_val:.0f} 极低 → 注意尾部风险")

    # 新闻分析
    high_impact = [n for n in briefing.get("top_news", [])
                   if n.get("impact_score", 0) > 0.3]
    if high_impact:
        implications.append(f"📰 {len(high_impact)} 条高影响新闻 → AI 决策时优先参考")

    # 内部人信号
    insider = briefing.get("insider_signals", [])
    buys = [i for i in insider if i.get("type") == "BUY"]
    sells = [i for i in insider if i.get("type") == "SELL"]
    if len(buys) > len(sells) * 2:
        implications.append(f"🟢 内部人净买入({len(buys)}买 vs {len(sells)}卖) → 看多信号")
    elif len(sells) > len(buys) * 2:
        implications.append(f"🔴 内部人净卖出({len(sells)}卖 vs {len(buys)}买) → 警惕信号")

    return implications


def _detect_risk_flags(briefing: dict) -> List[dict]:
    """检测风险信号"""
    flags = []

    # 检查恐慌信号
    sentiment = briefing.get("market_sentiment", {})
    vix = sentiment.get("vix", {})
    if vix.get("level") in ("HIGH", "EXTREME"):
        flags.append({
            "level": "HIGH",
            "type": "VIX_SPIKE",
            "message": f"VIX={vix.get('vix',0):.0f} 处于{vix.get('level')}水平",
            "action": "reduce_positions",
        })

    fg = sentiment.get("fear_greed", {})
    if fg.get("zone") == "EXTREME_FEAR":
        flags.append({
            "level": "MEDIUM",
            "type": "EXTREME_FEAR",
            "message": "市场极度恐惧，可能是买入机会也可能是陷阱",
            "action": "caution",
        })

    # 检查关键新闻
    for news in briefing.get("top_news", []):
        title = news.get("title", "").lower()
        if any(w in title for w in ["crash", "plunge", "panic", "crisis", "collapse"]):
            flags.append({
                "level": "HIGH",
                "type": "PANIC_NEWS",
                "message": f"恐慌性新闻: {news.get('title', '')[:100]}",
                "action": "pause_trading",
            })

        if any(w in title for w in ["fed", "rate hike", "inflation", "cpi", "fomc"]):
            flags.append({
                "level": "MEDIUM",
                "type": "MACRO_EVENT",
                "message": f"宏观事件: {news.get('title', '')[:100]}",
                "action": "reduce_size",
            })

    return flags[:5]


def briefing_to_prompt(briefing: dict) -> str:
    """将简报转为 AI prompt 格式（直接喂给 DeepSeek）"""
    lines = ["=== 实时市场情报简报 ===", f"时间: {briefing['timestamp']}", ""]

    # 情绪
    s = briefing.get("market_sentiment", {})
    lines.append("【市场情绪】")
    lines.append(f"  综合评分: {s.get('composite_score', 50)}/100 ({s.get('bias', 'NEUTRAL')})")
    lines.append(f"  建议: {s.get('advice', '')}")

    vix = s.get("vix", {})
    lines.append(f"  VIX: {vix.get('vix', 18):.1f} → {vix.get('trading_implication', '')}")

    fg = s.get("fear_greed", {})
    lines.append(f"  恐惧贪婪指数: {fg.get('score', 50)} ({fg.get('zone', 'NEUTRAL')})")
    lines.append("")

    # 新闻
    lines.append("【重要新闻头条】")
    for news in briefing.get("top_news", [])[:10]:
        impact = news.get("impact_score", 0)
        icon = "🔴" if impact > 0.4 else ("🟡" if impact > 0.2 else "⚪")
        lines.append(f"  {icon} [{impact:.0%}] {news.get('title', '')[:120]}")
    lines.append("")

    # 内部人
    insider = briefing.get("insider_signals", [])
    if insider:
        lines.append("【内部人交易信号】")
        buys = sum(1 for i in insider if i.get("type") == "BUY")
        sells = sum(1 for i in insider if i.get("type") == "SELL")
        lines.append(f"  净买入: {buys} | 净卖出: {sells}")
    lines.append("")

    # 交易建议
    lines.append("【交易建议】")
    for imp in briefing.get("trading_implications", []):
        lines.append(f"  {imp}")
    lines.append("")

    # 风险
    flags = briefing.get("risk_flags", [])
    if flags:
        lines.append("【风险预警】")
        for flag in flags:
            lines.append(f"  🚨 [{flag['level']}] {flag['message']} → {flag['action']}")
    lines.append("")

    return "\n".join(lines)


def quick_briefing() -> str:
    """快速生成简报文本（供日志/仪表盘使用）"""
    briefing = get_pre_trade_briefing(max_news=8)
    return briefing_to_prompt(briefing)


# ============================================================
# CLI
# ============================================================
def main():
    """命令行工具：打印当前简报"""
    print("📡 正在获取实时情报...")
    briefing = get_pre_trade_briefing(max_news=10)
    print(briefing_to_prompt(briefing))
    print(f"\n⏱ 生成耗时: {briefing['generated_in_seconds']:.1f}秒")


if __name__ == "__main__":
    main()
