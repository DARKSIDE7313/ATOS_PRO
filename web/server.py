"""
ATOS PRO Web API — FastAPI server
启动: /opt/homebrew/bin/python3 -m uvicorn web.server:app --host 0.0.0.0 --port 8000
"""
import json
import threading
from datetime import datetime
from pathlib import Path

import yfinance as yf
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from atos.strategy.rsi2_strategy import RSI2Strategy
from atos.strategy.bollinger_strategy import BollingerStrategy
from atos.strategy.dual_momentum import DualMomentumStrategy
from atos.strategy.alpha_engine import AlphaEngine
from atos.strategy.nighthawk import NighthawkEngine

app = FastAPI(title="ATOS PRO")
WEB_DIR = Path(__file__).parent

# Available strategies
STRATEGIES = {
    "alpha": "AlphaEngine 三合一 (67% WR, 9/10盈利)",
    "nighthawk": "Nighthawk 高胜率精选 (85-90% WR, 低频)",
    "rsi2": "RSI-2 极端反转 (60-70% WR, 高频)",
    "bollinger": "布林带均值回归 (55-65% WR, 低频)",
    "momentum": "双动量趋势 (50-65% WR, 长线)",
}

# Project root for reading data files
PROJECT_ROOT = WEB_DIR.parent


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB_DIR / "templates" / "index.html").read_text()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/live")
async def live_data():
    """Returns live portfolio dashboard data."""
    return _get_live_data()


@app.get("/api/performance")
async def performance_data():
    """Returns performance metrics (Sharpe, Sortino, Calmar, etc.)"""
    try:
        from atos.core.performance import get_tracker
        return get_tracker().get_metrics()
    except Exception:
        return {"error": "tracker not initialized"}


@app.get("/api/short")
async def short_data():
    """Shadow (short-term) portfolio only."""
    return _get_short_data()


@app.get("/api/long")
async def long_data():
    """Long-term portfolio only (包括 Phoenix v3)."""
    return _get_long_data()


@app.get("/api/ai")
async def ai_data():
    """AI insights and recent decisions."""
    return _get_ai_data()


@app.get("/api/trades")
async def trades_data():
    """Recent trade history."""
    return _get_trades_data()


@app.get("/api/strategies")
async def list_strategies():
    return STRATEGIES


@app.post("/api/run")
async def run_strategy(request: Request):
    body = await request.json()
    ticker = body.get("ticker", "NVDA").upper().strip()
    capital = body.get("capital", 100000)
    strategy = body.get("strategy", "rsi2")

    result = {}
    done = threading.Event()

    def _run():
        nonlocal result
        result = _sync_backtest(ticker, capital, strategy)
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    done.wait(timeout=120)
    return JSONResponse(result)


def _sync_backtest(ticker: str, capital: int, strategy: str) -> dict:
    """Dispatch to the selected strategy."""
    try:
        data = yf.download(ticker, period="2y", progress=False)
    except Exception as e:
        return {"error": f"yfinance download failed: {e}", "ticker": ticker}

    if data is None or data.empty:
        return {"error": f"No data for {ticker}", "ticker": ticker}

    closes = data["Close"]
    if isinstance(closes, pd.DataFrame):
        closes = closes.iloc[:, 0]

    close_values = closes.values.flatten()

    if strategy == "rsi2":
        return _run_rsi2(ticker, close_values, capital)
    elif strategy == "bollinger":
        return _run_bollinger(ticker, close_values, capital)
    elif strategy == "momentum":
        return _run_momentum(ticker, data, capital)
    elif strategy == "alpha":
        return _run_alpha(ticker, data, capital)
    elif strategy == "nighthawk":
        return _run_nighthawk(ticker, data, capital)
    else:
        return {"error": f"Unknown strategy: {strategy}", "ticker": ticker}


def _signals_to_result(ticker: str, capital: int, signals: list, close_values,
                       strategy_name: str) -> dict:
    """Convert raw strategy signals to the standard API response format."""
    trades = []
    signals_log = []
    price_series = []
    trade_count = 0

    for i, close in enumerate(close_values):
        price_series.append({
            "time": str(i),
            "close": round(float(close), 2),
            "volume": 0
        })

    for sig in signals:
        price = sig['price']
        action = sig['action']

        if action == 'BUY':
            in_position = True
            signals_log.append({
                "type": "BUY", "ticker": ticker,
                "price": round(price, 2),
                "reason": sig.get('reason', ''),
                "time": str(sig.get('time', ''))
            })
        elif action == 'SELL':
            trade_count += 1
            pnl_pct = sig.get('pnl_pct', 0)
            # Use position_pct from signal, default 10% of capital
            pos_pct = sig.get('position_pct', 0.10)
            pnl_dollar = pnl_pct * capital * pos_pct
            trades.append({
                "ticker": ticker, "side": "SELL",
                "price": round(price, 2),
                "pnl": round(pnl_dollar, 0),
                "pnl_pct": round(pnl_pct * 100, 1),
                "reason": sig.get('reason', ''),
                "time": str(sig.get('time', ''))
            })
            signals_log.append({
                "type": "SELL", "ticker": ticker,
                "price": round(price, 2),
                "pnl": round(pnl_dollar, 0),
                "reason": sig.get('reason', ''),
                "time": str(sig.get('time', ''))
            })
            in_position = False

    # Calculate stats
    total_pnl = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0

    report = f"""ATOS PRO — {strategy_name.upper()} 绩效报告
  股票: {ticker}  | 总交易: {len(trades)}
  胜率: {wr:.1f}%  | 净盈亏: ${total_pnl:,.0f}
  盈利次数: {len(wins)}  | 亏损次数: {len(losses)}"""

    return {
        "ticker": ticker,
        "strategy": strategy_name,
        "capital": capital,
        "trades": trades,
        "signals": signals_log,
        "price_data": price_series,
        "report": report,
        "total_trades": len(trades),
    }


def _run_rsi2(ticker: str, closes, capital: int) -> dict:
    s = RSI2Strategy()
    signals = s.generate_signals(ticker, closes,
                                 buy_threshold=15, exit_rsi=70,
                                 stop_pct=0.05, take_pct=0.05)
    return _signals_to_result(ticker, capital, signals, closes, "RSI-2")


def _run_bollinger(ticker: str, closes, capital: int) -> dict:
    s = BollingerStrategy()
    signals = s.generate_signals(ticker, closes,
                                 bb_period=20, bb_std=2.0,
                                 rsi_low=35, stop_pct=0.05, take_pct=0.08)
    return _signals_to_result(ticker, capital, signals, closes, "Bollinger")


def _run_momentum(ticker: str, data, capital: int) -> dict:
    s = DualMomentumStrategy()
    closes = data['Close'].values.flatten()
    vols = data['Volume'].values.flatten() if 'Volume' in data else None
    signals = s.generate_signals(ticker, closes, volumes=vols,
                                 short_window=63, long_window=252,
                                 stop_pct=0.08, take_pct=0.20)
    return _signals_to_result(ticker, capital, signals, closes, "Dual Momentum")


def _run_alpha(ticker: str, data, capital: int) -> dict:
    """AlphaEngine — ensemble strategy with 3 signal sources."""
    s = AlphaEngine()
    closes = data['Close'].values.flatten()
    highs = data['High'].values.flatten()
    lows = data['Low'].values.flatten()
    vols = data['Volume'].values.flatten()
    signals = s.generate_signals(ticker, closes, highs, lows, vols)
    return _signals_to_result(ticker, capital, signals, closes, "AlphaEngine")


def _run_nighthawk(ticker: str, data, capital: int) -> dict:
    """Nighthawk — ultra-high probability mean reversion."""
    s = NighthawkEngine()
    closes = data['Close'].values.flatten()
    highs = data['High'].values.flatten()
    lows = data['Low'].values.flatten()
    vols = data['Volume'].values.flatten()
    signals = s.generate_signals(ticker, closes, highs, lows, vols,
                                  top_pct=0.03, take_pct=0.025, stop_pct=0.015)
    return _signals_to_result(ticker, capital, signals, closes, "Nighthawk")


def _get_short_data() -> dict:
    """Shadow 短线组合数据"""
    path = PROJECT_ROOT / "data" / "shadow_state.json"
    if not path.exists():
        return {"error": "no data"}
    with open(path) as f:
        ss = json.load(f)
    positions = []
    pv = ss.get("equity", ss.get("cash", 0))
    for sym, pos in ss.get("positions", {}).items():
        price = pos.get("last_price", pos.get("avg_price", 0))
        qty = pos.get("qty", pos.get("shares", 0))
        val = qty * price
        pnl = (price - pos.get("avg_price", 0)) * qty
        positions.append({
            "sym": sym, "qty": qty,
            "avg": round(pos.get("avg_price", 0), 2),
            "price": round(price, 2),
            "value": round(val, 2), "pnl": round(pnl, 2),
            "return_pct": round((price/pos.get("avg_price", 1)-1)*100, 2),
            "weight": round(val/pv*100, 1) if pv > 0 else 0,
        })
    trades = []
    for t in ss.get("trade_history", [])[-30:]:
        trades.append({
            "time": t.get("date", ""), "symbol": t.get("symbol", ""),
            "action": t.get("action", ""), "shares": t.get("shares", 0),
            "price": t.get("price", 0), "reason": t.get("reason", ""),
        })
    return {
        "portfolio_value": round(pv, 2),
        "cash": round(ss.get("cash", 0), 2),
        "pnl": round(pv - ss.get("initial_cash", pv), 2),
        "return_pct": round((pv/ss.get("initial_cash", pv)-1)*100, 2),
        "cycles": ss.get("cycle_count", 0),
        "positions": positions,
        "trades": trades,
    }


def _get_long_data() -> dict:
    """长线组合（LongTerm + Phoenix v3）"""
    # 先读 longterm_state.json
    lt = PROJECT_ROOT / "data" / "longterm_state.json"
    holdings = []
    total_val = 0
    cash = 0
    if lt.exists():
        with open(lt) as f:
            ls = json.load(f)
        cash = ls.get("cash", 0)
        for sym, pos in ls.get("holdings", {}).items():
            price = pos.get("last_price", pos.get("avg_cost", 0))
            qty = pos.get("shares", 0)
            val = qty * price
            pnl = (price - pos.get("avg_cost", 0)) * qty
            total_val += val
            holdings.append({
                "sym": sym, "qty": qty,
                "avg": round(pos.get("avg_cost", 0), 2),
                "price": round(price, 2),
                "value": round(val, 2), "pnl": round(pnl, 2),
                "return_pct": round((price/pos.get("avg_cost", 1)-1)*100, 2),
                "score": pos.get("composite_score", 50),
                "source": "longterm",
            })
    # 补充 Phoenix v3 数据
    phx = PROJECT_ROOT.parent / "phoenix_state.json"
    if phx.exists():
        with open(phx) as f:
            ps = json.load(f)
        for sym, pos in ps.get("positions", {}).items():
            if sym in {h["sym"] for h in holdings}:
                continue
            price = pos.get("last_price", pos.get("avg_cost", 0))
            qty = pos.get("shares", 0)
            val = qty * price
            pnl = (price - pos.get("avg_cost", 0)) * qty
            total_val += val
            holdings.append({
                "sym": sym, "qty": qty,
                "avg": round(pos.get("avg_cost", 0), 2),
                "price": round(price, 2),
                "value": round(val, 2), "pnl": round(pnl, 2),
                "return_pct": round((price/pos.get("avg_cost", 1)-1)*100, 2),
                "source": "phoenix_v3",
            })
    pv = total_val + cash
    return {
        "portfolio_value": round(pv, 2),
        "cash": round(cash, 2),
        "pnl": round(pv - ps.get("total_deposited", 1000000), 2) if phx.exists() else round(total_val + cash - 1000000, 2),
        "holdings": holdings,
        "runs": ps.get("runs", 0) if phx.exists() else 0,
    }


def _get_ai_data() -> dict:
    """AI 决策历史"""
    import sqlite3
    db = PROJECT_ROOT / "data" / "ai_memory.db"
    decisions = []
    if db.exists():
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT symbol, action, confidence, factor_score, debate_summary, created_at "
            "FROM decisions ORDER BY id DESC LIMIT 30"
        ).fetchall()
        for r in rows:
            decisions.append({
                "symbol": r[0], "action": r[1],
                "confidence": round(r[2], 2) if r[2] else 0,
                "factor_score": round(r[3], 2) if r[3] else 0,
                "summary": (r[4] or "")[:120],
                "time": r[5] or "",
            })
        conn.close()
    # 也读 ai_decisions.json (v6)
    v6 = PROJECT_ROOT / "data" / "ai_decisions.json"
    v6_decisions = []
    if v6.exists():
        with open(v6) as f:
            v6d = json.load(f)
        if isinstance(v6d, dict) and "decisions" in v6d:
            for d in v6d["decisions"][-20:]:
                v6_decisions.append({
                    "symbol": d.get("symbol", ""),
                    "verdict": d.get("verdict", ""),
                    "confidence": d.get("confidence", 0),
                    "reason": str(d.get("reason", ""))[:120],
                    "time": d.get("time", ""),
                })
    return {
        "v5_count": len(decisions),
        "v6_count": len(v6_decisions),
        "total": 312,  # from DB
        "recent_v5": decisions[-15:],
        "recent_v6": v6_decisions[-15:],
    }


def _get_trades_data() -> dict:
    """最近交易记录（从 trades.log 和 shadow_state）"""
    # 从 trades.log
    trades = []
    tlog = PROJECT_ROOT / "logs" / "trades.log"
    if tlog.exists():
        for line in tlog.read_text().split("\n")[-50:]:
            if "TRADE" in line:
                parts = line.split("|")
                if len(parts) >= 6:
                    trades.append({
                        "time": parts[0].strip(),
                        "action": parts[3].strip() if len(parts) > 3 else "",
                        "symbol": parts[4].strip() if len(parts) > 4 else "",
                        "reason": parts[-1].strip()[:80] if parts[-1] else "",
                    })
    # 计算统计
    wins = sum(1 for t in trades if "止盈" in t.get("reason", "") or "部分止盈" in t.get("reason", ""))
    losses = sum(1 for t in trades if "止损" in t.get("reason", "") or "轮动" in t.get("reason", ""))
    return {
        "recent": trades[-20:],
        "total": len(trades),
        "win_trades": wins,
        "loss_trades": losses,
    }

def _get_live_data() -> dict:
    """v10: 从真实状态文件读取实时数据"""
    # Read shadow state
    shadow_path = PROJECT_ROOT / "data" / "shadow_state.json"
    shadow_positions = []
    shadow_value = 0
    shadow_cash = 0
    shadow_pnl = 0
    shadow_cycles = 0
    shadow_equity = 0
    shadow_last_cycle = ""
    if shadow_path.exists():
        try:
            with open(shadow_path) as f:
                ss = json.load(f)
            shadow_cash = ss.get("cash", 0)
            shadow_cycles = ss.get("cycle_count", 0)
            shadow_equity = ss.get("equity", shadow_cash)
            shadow_last_cycle = ss.get("last_cycle", "")
            for sym, pos in ss.get("positions", {}).items():
                price = pos.get("last_price", pos.get("avg_price", 0))
                qty = pos.get("qty", 0)
                val = qty * price
                pnl = (price - pos.get("avg_price", 0)) * qty
                shadow_value += val
                shadow_pnl += pnl
                shadow_positions.append({
                    "sym": sym, "shares": qty,
                    "avg": round(pos.get("avg_price", 0), 2),
                    "price": round(price, 2),
                    "value": round(val, 2), "pnl": round(pnl, 2),
                    "return": round((price/pos.get("avg_price",1)-1)*100, 2) if pos.get("avg_price", 0) > 0 else 0,
                    "day_chg": 0, "day_chg_pct": 0,
                    "weight": round(val/shadow_equity*100, 1) if shadow_equity > 0 else 0,
                })
            if not shadow_positions and shadow_equity == 0:
                shadow_equity = shadow_cash
        except Exception:
            shadow_cash = 0

    short_return = (shadow_equity - (ss.get("initial_cash", shadow_equity))) / ss.get("initial_cash", 1) * 100 if shadow_path.exists() else 0

    # Read long-term holdings from old state (过渡期，等Phoenix接管)
    long_path = PROJECT_ROOT / "data" / "longterm_state.json"
    long_holdings = []
    long_value = 0
    long_cash = 0
    long_pnl = 0
    long_last_rebalance = ""
    if long_path.exists():
        try:
            with open(long_path) as f:
                ls = json.load(f)
            long_cash = ls.get("cash", 0)
            long_last_rebalance = ls.get("last_rebalance", "")
            for sym, pos in ls.get("holdings", {}).items():
                price = pos.get("last_price", pos.get("avg_cost", 0))
                qty = pos.get("shares", 0)
                val = qty * price
                pnl = (price - pos.get("avg_cost", 0)) * qty
                pnl_pct = (price/pos.get("avg_cost",1)-1)*100 if pos.get("avg_cost", 0) > 0 else 0
                long_value += val
                long_pnl += pnl
                long_holdings.append({
                    "sym": sym, "shares": qty,
                    "avg": round(pos.get("avg_cost", 0), 2),
                    "price": round(price, 2),
                    "value": round(val, 2), "pnl": round(pnl, 2),
                    "return": round(pnl_pct, 2),
                    "score": pos.get("composite_score", 50),
                    "weight": round(val/(long_value or 1)*100, 1),
                })
        except Exception:
            long_cash = 0

    long_return = (long_pnl / (long_value - long_pnl)) * 100 if (long_value - long_pnl) > 0 else 0

    # Read trade log
    trade_log_path = PROJECT_ROOT / "data" / "trade_log.jsonl"
    trades = []
    if trade_log_path.exists():
        for line in trade_log_path.read_text().strip().split("\n"):
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    combined_pv = shadow_equity + long_value + long_cash
    combined_pnl = shadow_pnl + long_pnl
    from atos.config_shared import TOTAL_CAPITAL
    combined_return = (combined_pv - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100

    # Build activity log from trade history
    activity_log = []
    if shadow_path.exists():
        try:
            with open(shadow_path) as f:
                ss = json.load(f)
            for t in ss.get("trade_history", [])[-8:]:
                activity_log.append({
                    "time": t.get("date", ""),
                    "msg": f"{t.get('symbol','')} {t.get('action','')} {t.get('shares',0)} @ ${t.get('price',0):.2f} — {t.get('reason','')}",
                })
            activity_log.append({
                "time": shadow_last_cycle,
                "msg": f"Shadow cycle #{shadow_cycles} — Equity ${shadow_equity:,.0f}, {len(shadow_positions)} positions"
            })
            if long_last_rebalance:
                activity_log.append({
                    "time": long_last_rebalance,
                    "msg": f"Long-term rebalance — {len(long_holdings)} holdings, cash ${long_cash:,.0f}"
                })
        except Exception:
            pass

    # v11: Add US market time
    import datetime as _dt
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    is_dst = now_utc.month > 3 and now_utc.month < 11
    et_offset = -4 if is_dst else -5
    et_now = now_utc + _dt.timedelta(hours=et_offset)
    et_hour = et_now.hour + et_now.minute / 60
    et_day = et_now.weekday()
    is_weekday = 0 <= et_day <= 4
    mkt_open = 9.5 <= et_hour <= 16.0 and is_weekday
    market_time = {
        "us_eastern": et_now.strftime("%H:%M:%S"),
        "timezone": "EDT" if is_dst else "EST",
        "day_of_week": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][et_day],
        "market_open": mkt_open,
        "next_open": "Monday 9:30 AM ET" if et_day >= 4 else "Tomorrow 9:30 AM ET" if et_hour >= 16 else "Today 9:30 AM ET" if et_hour < 9.5 else "Now",
    }

    return {
        "market_time": market_time,
        "overview": {
            "combined_pv": round(combined_pv, 2),
            "combined_pnl": round(combined_pnl, 2),
            "return_pct": round(combined_return, 2),
            "initial_capital": TOTAL_CAPITAL,
            "total_cash": round(shadow_cash + long_cash, 2),
            "short": {
                "value": round(shadow_value, 2), "cash": round(shadow_cash, 2),
                "pnl": round(shadow_pnl, 2), "return_pct": round(short_return, 2),
                "positions": len(shadow_positions), "cycles": shadow_cycles,
            },
            "long": {
                "value": round(long_value, 2), "cash": round(long_cash, 2),
                "pnl": round(long_pnl, 2), "return_pct": round(long_return, 2),
                "holdings": len(long_holdings),
                "rebalance": long_last_rebalance,
            },
        },
        "short_term": {
            "portfolio_value": round(shadow_equity, 2),
            "pnl": round(shadow_pnl, 2),
            "return_pct": round(short_return, 2),
            "cash": round(shadow_cash, 2),
            "positions_count": len(shadow_positions),
            "positions": shadow_positions,
            "system": {"cycles": shadow_cycles, "last_cycle": shadow_last_cycle, "equity": round(shadow_equity, 2)},
        },
        "long_term": {
            "portfolio_value": round(long_value + long_cash, 2),
            "pnl": round(long_pnl, 2),
            "return_pct": round(long_return, 2),
            "cash": round(long_cash, 2),
            "holdings_count": len(long_holdings),
            "holdings": long_holdings,
            "strategy": {"rebalance": long_last_rebalance, "cash": round(long_cash, 2)},
        },
        "stops": [],
        "trades": trades[-20:] if trades else [],
        "activity_log": activity_log,
    }
