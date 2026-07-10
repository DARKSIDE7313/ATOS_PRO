"""
ATOS PRO v3 — 数据层
====================
Futu OpenD 行情 + 基本面 + K线。Futu优先，yfinance后备。
"""
from atos.data.futu_provider import (
    FutuProvider, get_futu,
    get_stock_info, get_quote, get_snapshots,
    get_fundamentals, get_kline,
)
