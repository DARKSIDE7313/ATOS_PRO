"""
ATOS PRO v3 — Phoenix 实时看板
=================================
轻量级，不依赖 Web 服务器。直接读取 state 文件 + Futu 实时数据。

用法:
  python -m atos.longterm.dashboard_live           # 打印一次
  python -m atos.longterm.dashboard_live --watch   # 每10秒刷新
"""

import os, sys, json, datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "phoenix_state.json")


def get_futu_price(ticker: str) -> float:
    """快速获取实时价格"""
    try:
        from atos.data.futu_provider import get_quote
        q = get_quote(ticker)
        return q.get("price", 0) if q.get("valid") else 0
    except Exception:
        return 0


def render():
    # 读状态
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        print("状态文件不可读")
        return

    positions = state.get("positions", {})
    cash = state.get("cash", 0)
    runs = state.get("runs", 0)
    phase = state.get("market_phase", "?")
    last_run = state.get("last_full_run", "?")[:19] if state.get("last_full_run") else "?"

    # 计算实时市值
    total_market_value = 0.0
    total_cost = 0.0
    pos_list = []
    for sym, pos in positions.items():
        shares = pos.get("shares", 0)
        avg = pos.get("avg_cost", 0)
        cost = shares * avg
        price = get_futu_price(sym)
        if price <= 0:
            price = avg
        value = shares * price
        pnl = value - cost
        pnl_pct = (price - avg) / avg * 100 if avg > 0 else 0
        total_market_value += value
        total_cost += cost
        pos_list.append({
            "sym": sym, "layer": pos.get("layer",""), "shares": shares,
            "avg": avg, "price": price, "value": value, "pnl_pct": pnl_pct,
        })

    pos_list.sort(key=lambda x: -abs(x["value"]))

    total_value = cash + total_market_value
    total_pnl = total_value - state.get("total_deposited", cash)
    total_pnl_pct = total_pnl / state.get("total_deposited", 1) * 100

    trades = state.get("trade_history", [])[-10:]

    # 渲染
    os.system("clear" if sys.platform != "win32" else "cls")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 80)
    print(f"  🔥 ATOS Phoenix 实时看板 — {now} — 第{runs}次运行")
    print(f"  市场: {phase} | 上次: {last_run}")
    print("=" * 80)

    print(f"\n  💰 总资产:  ${total_value:>12,.0f}  |  现金: ${cash:>10,.0f}")
    print(f"  📈 总盈亏:  {total_pnl:>+12,.0f}  ({total_pnl_pct:+.2f}%)")
    if pos_list:
        print(f"  📦 持仓:    {len(pos_list)} 只  |  市值: ${total_market_value:>10,.0f}")
    print()

    if pos_list:
        print(f"  {'代码':<8} {'层':<8} {'股数':>8} {'成本':>10} {'现价':>10} {'市值':>12} {'盈亏%':>8}")
        print("  " + "-" * 74)
        for p in pos_list[:20]:
            pnl_mark = "🟢" if p["pnl_pct"] > 0 else "🔴" if p["pnl_pct"] < 0 else "⚪"
            print(f"  {pnl_mark} {p['sym']:<6} {p['layer']:<8} {p['shares']:>8} "
                  f"${p['avg']:>9.2f} ${p['price']:>9.2f} ${p['value']:>11,.0f} "
                  f"{p['pnl_pct']:>+7.2f}%")
    else:
        print("  📭 暂无持仓 — 等待策略信号...")

    if trades:
        print(f"\n  📜 最近交易:")
        for t in trades[-5:]:
            action_icon = "🟢买" if t.get("action") == "BUY" else "🔴卖"
            print(f"    {action_icon} {t.get('symbol','?'):<6} "
                  f"{t.get('shares',0)}股 @ ${t.get('price',0):.2f} "
                  f"({t.get('date','')[:16]})")

    print("\n" + "=" * 80)
    print("  自动交易守护进程: " +
          ("🟢 运行中" if os.path.exists(os.path.join(os.path.dirname(STATE_FILE), "data", ".phoenix_daemon.pid"))
           else "🔴 未运行"))
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Phoenix 实时看板")
    parser.add_argument("--watch", action="store_true", help="每10秒自动刷新")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                render()
                time.sleep(10)
        except KeyboardInterrupt:
            print("\n👋 看板已关闭")
    else:
        render()
