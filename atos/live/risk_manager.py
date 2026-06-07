MAX_DAILY_LOSS_PCT = 0.03
STOP_LOSS_PCT      = 0.05
TAKE_PROFIT_PCT    = 0.15
MAX_ORDERS_PER_DAY = 20
_daily_pnl   = 0.0
_order_count = 0

def reset_daily():
    global _daily_pnl, _order_count
    _daily_pnl = 0.0; _order_count = 0

def record_fill(pnl: float):
    global _daily_pnl, _order_count
    _daily_pnl += pnl; _order_count += 1

def check_stop_losses(positions, account_state):
    forced = []
    for p in positions:
        if p["pnl_pct"] <= -STOP_LOSS_PCT:
            forced.append({"action": "SELL", "symbol": p["symbol"],
                           "qty": p["qty"], "reason": "STOP_LOSS"})
    return forced

def filter_orders(proposed, account_state, regime):
    total    = account_state["total"]
    cash     = account_state["cash"]
    min_cash = account_state["constraints"]["min_cash"]
    max_pct  = account_state["constraints"]["max_single_pct"]
    safe     = []
    if total > 0 and _daily_pnl / total <= -MAX_DAILY_LOSS_PCT:
        print("[risk] Daily loss limit reached - no new opens")
        return [o for o in proposed if o["action"] == "SELL"]
    if _order_count >= MAX_ORDERS_PER_DAY:
        print("[risk] Max daily orders reached")
        return [o for o in proposed if o["action"] == "SELL"]
    bear_blocked = set()
    if regime.get("regime") in ("BEAR", "HIGH_VOL"):
        bear_blocked = {"TSLA", "AMD", "META", "NVDA"}
    for order in proposed:
        sym = order["symbol"]; action = order["action"]
        target_pct = order.get("target_pct", 0)
        if action == "SELL":
            safe.append(order); continue
        if sym in bear_blocked:
            print(f"[risk] Blocking {sym} - bear regime"); continue
        if target_pct > max_pct:
            order = dict(order, target_pct=max_pct)
        cost_est = total * order["target_pct"]
        if cash - cost_est < min_cash:
            print(f"[risk] Skipping {sym} - would breach min cash"); continue
        safe.append(order)
    return safe
