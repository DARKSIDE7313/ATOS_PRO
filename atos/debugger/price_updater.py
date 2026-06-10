#!/usr/bin/env python3
"""
ATOS 价格更新器 — 同时更新短线/长线 state 中的 last_price
每 5 分钟运行一次。通过 yfinance 获取最新价格。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from atos.core.logging import get_logger

logger = get_logger("price_updater")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHORT_FILE = "/Users/benson/ATOS_PRO/data/shadow_state.json"
LONG_FILE = "/Users/benson/ATOS_PRO/data/longterm_state.json"

def safe_price(v):
    try:
        v = float(v)
        if str(v) == "nan": return 0.0
        return v
    except: return 0.0


def update_prices(state_file, pos_key, price_key, equity_key):
    """从 yfinance 更新持仓的最新产品价格"""
    if not os.path.exists(state_file):
        return "missing"
    
    # 带重试读取
    state = None
    for retry in range(3):
        try:
            with open(state_file) as f:
                state = json.load(f)
            break
        except Exception:
            time.sleep(1)
    if state is None:
        return "noread"
    
    positions = state.get(pos_key, {}) or {}
    if not positions:
        return "nopos"
    
    import yfinance as yf
    updated = 0
    for sym in list(positions.keys()):
        try:
            t = yf.Ticker(sym)
            info = t.info or {}
            px = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0
            if not px or str(px) == "nan":
                continue
            px = float(px)
            if px <= 0:
                continue
            if isinstance(positions[sym], dict):
                positions[sym][price_key] = px
                updated += 1
        except Exception:
            continue
    
    # 重算总权益
    if pos_key == "positions":
        cash = state.get("cash", 0)
        total = cash + sum(
            p.get("qty", 0) * safe_price(p.get(price_key, 0))
            for p in positions.values() if isinstance(p, dict)
        )
        state["equity"] = total
    elif pos_key == "holdings":
        cash = state.get("cash", 0)
        total = cash + sum(
            p.get("shares", 0) * safe_price(p.get(price_key, 0))
            for p in positions.values() if isinstance(p, dict)
        )
        state["total_value"] = total
    
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
    
    return f"ok:{updated}"


def main():
    logger.info("价格更新器启动")
    r1 = update_prices(SHORT_FILE, "positions", "last_price", "equity")
    logger.info(f"短线: {r1}")
    r2 = update_prices(LONG_FILE, "holdings", "last_price", "total_value")
    logger.info(f"长线: {r2}")


if __name__ == "__main__":
    main()
