"""
ATOS 每日收益追踪模块
每次周期结束时调用 record_daily()，自动记录当天收益。
Dashboard 可读取 data/daily_returns/ 获取历史收益曲线。
"""
import os, json, datetime

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAILY_DIR = os.path.join(BASE, "data", "daily_returns")
os.makedirs(DAILY_DIR, exist_ok=True)

_last_equity = None

def record_daily(equity: float, trades_today: int = 0, positions: int = 0):
    """每次周期结束时调用，自动记录当天收益"""
    global _last_equity
    today = datetime.date.today().isoformat()
    filepath = os.path.join(DAILY_DIR, f"{today}.json")

    # 加载今天的记录
    if os.path.exists(filepath):
        with open(filepath) as f:
            rec = json.load(f)
    else:
        # 新的一天，用上一次存下的权益作为起始
        start_eq = _last_equity if _last_equity else equity
        rec = {
            "date": today,
            "start_equity": start_eq,
            "end_equity": equity,
            "daily_pnl": 0,
            "daily_return_pct": 0,
            "trades_today": 0,
            "positions_end": 0,
        }

    rec["end_equity"] = equity
    rec["daily_pnl"] = round(equity - rec["start_equity"], 2)
    rec["daily_return_pct"] = round((equity - rec["start_equity"]) / rec["start_equity"] * 100, 4) if rec["start_equity"] > 0 else 0
    rec["trades_today"] = trades_today
    rec["positions_end"] = positions
    rec["last_update"] = datetime.datetime.now().isoformat()

    with open(filepath, "w") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)

    _last_equity = equity
    return rec


def get_history(days: int = 30) -> list:
    """获取最近 N 天的收益率历史"""
    results = []
    files = sorted([f for f in os.listdir(DAILY_DIR) if f.endswith('.json')], reverse=True)[:days]
    for fn in files:
        try:
            with open(os.path.join(DAILY_DIR, fn)) as f:
                results.append(json.load(f))
        except:
            pass
    return sorted(results, key=lambda r: r.get("date", ""))


def get_summary() -> dict:
    """Dashboard 用的汇总数据"""
    history = get_history(90)
    if not history:
        return {"total_days": 0, "win_days": 0, "loss_days": 0, "win_rate": 0,
                "cumulative_return": 0, "best_day": 0, "worst_day": 0, "history": []}

    wins = sum(1 for r in history if r.get("daily_return_pct", 0) > 0)
    losses = sum(1 for r in history if r.get("daily_return_pct", 0) < 0)
    cum = 1.0
    for r in history:
        cum *= (1 + r.get("daily_return_pct", 0) / 100)
    cum_return = round((cum - 1) * 100, 2)
    best = max(r.get("daily_return_pct", 0) for r in history)
    worst = min(r.get("daily_return_pct", 0) for r in history)

    return {
        "total_days": len(history),
        "win_days": wins,
        "loss_days": losses,
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
        "cumulative_return": cum_return,
        "best_day": best,
        "worst_day": worst,
        "history": history[-30:],
    }
