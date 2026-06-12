"""
每日收盘复盘 — 收集当日交易记录 + 账户状态，
发给本地 DeepSeek-R1 分析，结果写入 reports/ 并自动更新 strategy_config.json。
用法: python daily_review.py
推荐: crontab 每天 20:05 自动跑
  5 20 * * 1-5 /Users/benson/ATOS_PRO/venv/bin/python /Users/benson/ATOS_PRO/atos/live/daily_review.py
"""
import os, sys, json, datetime

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

from atos.live.local_reviewer import run_review

LOG_PATH    = os.path.join(BASE, "data", "trade_log.jsonl")
CONFIG_PATH = os.path.join(BASE, "data", "strategy_config.json")


def load_today_trades():
    today = datetime.date.today().isoformat()
    trades = []
    if not os.path.exists(LOG_PATH):
        return trades
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("date", "")[:10] == today:
                    trades.append(rec)
            except Exception:
                pass
    return trades


def load_market_summary():
    summary_path = os.path.join(BASE, "data", "market_summary.json")
    if os.path.exists(summary_path):
        try:
            with open(summary_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"note": "no market_summary.json found"}


def log_trade(symbol, action, qty, price, pnl_pct=None):
    """供 live_trader.py 调用：每次平仓后记录一笔。"""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    record = {
        "date":    datetime.datetime.now().isoformat(),
        "symbol":  symbol,
        "action":  action,
        "qty":     qty,
        "price":   price,
        "pnl_pct": pnl_pct,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if pnl_pct is not None:
        try:
            from atos.live.kelly import save_trade
            save_trade(pnl_pct)
        except Exception as e:
            print(f"[daily_review] save_trade error: {e}")


def main():
    print(f"[daily_review] 开始复盘 {datetime.date.today()}")
    trades  = load_today_trades()
    summary = load_market_summary()
    print(f"[daily_review] 今日成交: {len(trades)} 笔")

    # 账户状态（无法连接 Futu 时用占位）
    try:
        from atos.live.portfolio import get_account_state
        account = get_account_state()
    except Exception as e:
        print(f"[daily_review] 账户获取失败(用占位): {e}")
        account = {"total": 1000000, "cash": 300000, "positions": []}

    # 读取最近7天复盘摘要作为历史记忆
    import glob
    history = []
    for rp in sorted(glob.glob(os.path.join(BASE, "reports", "review_*.json")))[-7:]:
        try:
            with open(rp, encoding="utf-8") as f:
                r = json.load(f)
            history.append({
                "date": os.path.basename(rp)[7:17],
                "performance_summary": r.get("performance_summary", ""),
                "strategy_adjustments": r.get("strategy_adjustments", []),
                "confidence_score": r.get("confidence_score", ""),
            })
        except Exception:
            pass
    summary["history_last_7_days"] = history

    result = run_review(
        trade_log      = trades,
        market_summary = summary,
        account_state  = account,
    )

    print("\n===== 复盘结果 =====")
    print(f"  表现:     {result.get('performance_summary', 'N/A')}")
    print(f"  做对:     {result.get('what_worked', [])}")
    print(f"  做错:     {result.get('what_failed', [])}")
    print(f"  参数调整: {result.get('strategy_adjustments', [])}")
    print(f"  明日展望: {result.get('market_outlook', 'N/A')}")
    print(f"  置信度:   {result.get('confidence_score', 'N/A')}")
    print("===================\n")

    # === LLM Wiki 自动入库 ===
    try:
        import json as _json
        from pathlib import Path as _Path
        _report_path = _Path(BASE) / "reports" / f"review_{datetime.date.today()}.json"
        _report_path.parent.mkdir(parents=True, exist_ok=True)
        _report_path.write_text(_json.dumps(result, ensure_ascii=False, indent=2))
        print(f"[daily_review] 报告已保存: {_report_path}")

        sys.path.insert(0, os.path.expanduser("~/llm-wiki/ops/scripts"))
        from wiki_tools import wiki_ingest_atos
        wiki_result = wiki_ingest_atos(str(_report_path))
        if wiki_result.get("ok"):
            if wiki_result.get("has_alerts"):
                print(f"[wiki] ⚠️  异常单已入库: {wiki_result.get('actions')}")
            else:
                print(f"[wiki] 日报已入库: {wiki_result['ingested']}")
        else:
            print(f"[wiki] 入库失败: {wiki_result.get('error')}")
    except Exception as e:
        print(f"[wiki] 入库异常: {e}")


if __name__ == "__main__":
    main()
