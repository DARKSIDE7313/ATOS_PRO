"""
DeepSeek 云端 API 每日复盘模块。
收盘后分析当日交易记录，输出策略调整建议。
API Key 放在环境变量 DEEPSEEK_API_KEY 里。
"""
import json, os, datetime, requests

API_URL = "https://api.deepseek.com/chat/completions"
MODEL   = "deepseek-chat"   # 先用 chat 模型，支持 json_object response_format
# 若想用 R1 深度推理，可改为 "deepseek-reasoner"，但需删除下面第68行的 response_format
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

DATA_PATH   = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
REVIEW_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "reports")

SYSTEM_PROMPT = """
You are a quantitative trading analyst. Think step by step before answering.

STEP 1 - CALCULATE FIRST:
- Count total trades, wins (pnl_pct > 0), losses (pnl_pct <= 0)
- Win rate = wins / total
- Avg win pnl_pct, avg loss pnl_pct, win/loss ratio
- Total expected return = sum of all pnl_pct

STEP 2 - COMPARE TO HISTORY (if provided in market_summary.history_last_7_days):
- Is today's win rate better or worse than historical average?
- Is drawdown within normal range?
- What patterns repeat across multiple days?

STEP 3 - OUTPUT this exact JSON:
{
  "performance_summary": "Must include: X trades, Y wins, Z losses, win_rate=%, total_pnl=%",
  "what_worked": ["specific observations with numbers"],
  "what_failed": ["specific observations with numbers"],
  "strategy_adjustments": [
    {"parameter": "stop_loss_pct", "current": 0.05, "suggested": 0.04, "reason": "cite specific data"}
  ],
  "market_outlook": "specific outlook for next session",
  "confidence_score": 0.0
}
RULES:
- Only suggest parameter changes if at least 3 trades support it
- Never hallucinate trade counts or prices
- If history shows same mistake 3+ days in a row, flag it as CRITICAL
"""


def run_review(trade_log: list, market_summary: dict, account_state: dict) -> dict:
    if not API_KEY:
        print("[reviewer] ERROR: DEEPSEEK_API_KEY not set")
        return {"error": "DEEPSEEK_API_KEY not set"}

    payload_data = {
        "date":           datetime.date.today().isoformat(),
        "trade_log":      trade_log,
        "market_summary": market_summary,
        "account": {
            "total":     account_state.get("total"),
            "cash":      account_state.get("cash"),
            "positions": account_state.get("positions", []),
        },
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": json.dumps(payload_data, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type":  "application/json",
    }
    try:
        print("[reviewer] Sending to DeepSeek R1 cloud API...")
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        review  = json.loads(content)
        _save_review(review)
        _apply_suggestions(review.get("strategy_adjustments", []))
        return review
    except Exception as e:
        print(f"[reviewer] Error: {e}")
        return {"error": str(e)}


def _save_review(review: dict):
    os.makedirs(REVIEW_PATH, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    path = os.path.join(REVIEW_PATH, f"review_{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(review, f, indent=2, ensure_ascii=False)
    print(f"[reviewer] Saved: {path}")


def _apply_suggestions(adjustments: list):
    config_path = os.path.join(DATA_PATH, "strategy_config.json")
    os.makedirs(DATA_PATH, exist_ok=True)
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {
            "stop_loss_pct": 0.05, "take_profit_pct": 0.15,
            "max_single_pct": 0.30, "rsi_overbought": 75,
            "rsi_oversold": 30, "kelly_win_rate": 0.70,
            "kelly_win_loss_r": 3.0, "last_updated": "",
            "adjustment_history": []
        }
    BOUNDS = {
        "stop_loss_pct":    (0.02, 0.12),
        "take_profit_pct":  (0.05, 0.30),
        "max_single_pct":   (0.05, 0.30),
        "rsi_overbought":   (65,   85),
        "rsi_oversold":     (15,   40),
        "kelly_win_rate":   (0.35, 0.85),
        "kelly_win_loss_r": (1.0,  6.0),
    }
    # v10: 防振荡机制
    # 1. 计算每个参数距离上次调整的天数
    history = config.get("adjustment_history", [])
    param_last_adjusted = {}
    for h in history:
        p = h.get("parameter")
        d = h.get("date", "")
        if p and d:
            param_last_adjusted[p] = d  # 记录最后一次

    # 2. 检查哪些参数在上次调整后导致了更好/更坏的结果
    #    如果调整后仍然 0 交易 → 回滚
    param_directions = {}  # {param: [(date, old, new, direction)]}
    for h in history:
        p = h.get("parameter")
        if p:
            old = h.get("old", 0)
            new = h.get("new", 0)
            param_directions.setdefault(p, []).append((h.get("date",""), old, new))

    for adj in adjustments:
        param   = adj.get("parameter")
        suggest = adj.get("suggested")
        if param not in BOUNDS or suggest is None:
            continue

        # v10: 7天冷却 — 同参数7天内不重复调整
        today = datetime.date.today().isoformat()
        last_date = param_last_adjusted.get(param, "")
        if last_date:
            try:
                last_dt = datetime.date.fromisoformat(last_date)
                days_since = (datetime.date.today() - last_dt).days
                if days_since < 7:
                    print(f"[reviewer] ⏭ {param}: 7天冷却中 (上次调整于 {last_date}, {days_since}天前)")
                    continue
            except Exception:
                pass

        # v10: 如果这个参数最近被调过，检查方向是否在振荡
        recent = param_directions.get(param, [])
        if len(recent) >= 2:
            # 检查最近两次是否方向相反
            prev_dir = recent[-1][2] - recent[-1][1]  # 上一次的 new - old
            this_dir = float(suggest) - config.get(param, 0)
            if prev_dir * this_dir < 0:  # 方向相反 → 振荡!
                print(f"[reviewer] 🔄 {param}: 检测到振荡! 上次{prev_dir:+.3f} 本次{this_dir:+.3f} — 减半幅度")
                # 减半调整幅度
                suggest = config[param] + this_dir * 0.25  # 只取25%的建议幅度

        lo, hi  = BOUNDS[param]
        clamped = max(lo, min(hi, float(suggest)))
        old_val = config.get(param)
        # v10: 最小调整阈值 — 变化小于1%则跳过
        if old_val and abs(clamped - old_val) / old_val < 0.01:
            print(f"[reviewer] ⏭ {param}: 变化太小 ({old_val}→{clamped})")
            continue
        config[param] = clamped
        config["adjustment_history"].append({
            "date": today,
            "parameter": param, "old": old_val,
            "new": clamped, "reason": adj.get("reason", ""),
        })
        print(f"[reviewer] {param}: {old_val} -> {clamped}")
    config["last_updated"] = datetime.date.today().isoformat()
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
