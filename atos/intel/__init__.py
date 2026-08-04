"""
ATOS Intel — 实时多源情报引擎
=============================
在 AI 做出交易决策前，提供最新的市场情报。

数据源:
  - Yahoo Finance RSS (新闻标题)
  - Finnhub API (新闻+情绪+内部人交易)
  - FRED (宏观经济数据)
  - Fear & Greed Index (市场情绪)
  - Earnings Calendar (财报日历)
  - SEC EDGAR (监管文件)

用法:
  from atos.intel.briefing import get_pre_trade_briefing
  briefing = get_pre_trade_briefing()
  # briefing 直接喂给 AI 决策引擎
"""

__version__ = "1.0.0"
