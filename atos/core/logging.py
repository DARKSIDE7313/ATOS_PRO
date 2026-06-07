"""
ATOS PRO v2 — 机构级日志系统
===========================
结构化日志，同时输出到控制台和文件。
支持 INFO/WARNING/ERROR/TRADE 四个级别。
交易日志自动包含时间戳、交易对、数量、价格。
"""

import logging
import os
import datetime
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs"
)
os.makedirs(LOG_DIR, exist_ok=True)

# 主日志器
_logger = logging.getLogger("atos")
_logger.setLevel(logging.DEBUG)
_logger.handlers.clear()

# 格式
_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 控制台输出
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(_formatter)
_logger.addHandler(_console)

# 文件输出（按日期轮转，最多保留30天）— INFO级别，不记录DEBUG
_file = RotatingFileHandler(
    os.path.join(LOG_DIR, f"atos_{datetime.date.today().strftime('%Y%m%d')}.log"),
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=30,
)
_file.setLevel(logging.INFO)
_file.setFormatter(_formatter)
_logger.addHandler(_file)

# 交易专用日志
_trade_logger = logging.getLogger("atos.trade")
_trade_logger.setLevel(logging.DEBUG)
_trade_file = RotatingFileHandler(
    os.path.join(LOG_DIR, "trades.log"),
    maxBytes=50 * 1024 * 1024,
    backupCount=90,
)
_trade_file.setFormatter(_formatter)
_trade_logger.addHandler(_trade_file)


def get_logger(name: str = "atos") -> logging.Logger:
    """获取模块日志器"""
    return logging.getLogger(f"atos.{name}")


def log_trade(symbol: str, side: str, qty: int, price: float,
              pnl: float = None, reason: str = ""):
    """记录交易到专用日志（结构化）"""
    pnl_str = f"${pnl:,.2f}" if pnl is not None else "N/A"
    _trade_logger.info(
        f"TRADE | {side:4s} | {symbol:6s} | {qty:4d}股 | "
        f"${price:,.2f} | PnL={pnl_str} | {reason}"
    )


def log_signal(symbol: str, signal: dict):
    """记录信号到调试日志"""
    _logger.debug(
        f"SIGNAL | {symbol:6s} | price={signal.get('price', 0):.2f} | "
        f"trend={signal.get('trend', '?')} | rsi={signal.get('rsi', 0):.1f}"
    )


def log_risk(event: str, detail: str = ""):
    """记录风控事件"""
    _logger.warning(f"RISK | {event} | {detail}")


def log_error(module: str, error: str):
    """记录错误"""
    _logger.error(f"ERROR | {module} | {error}")
