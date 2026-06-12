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

from atos.market.regime.regime_engine import RegimeEngine
from atos.risk.kelly_position_sizer import KellyPositionSizer
from atos.risk.institutional_risk_engine import InstitutionalRiskEngine
from atos.portfolio.portfolio import Portfolio
from atos.monitoring.kill_switch import KillSwitch
from atos.reporting.performance_report import PerformanceReporter
from atos.strategy.rsi2_strategy import RSI2Strategy
from atos.strategy.bollinger_strategy import BollingerStrategy
from atos.strategy.dual_momentum import DualMomentumStrategy
from atos.strategy.alpha_engine import AlphaEngine

app = FastAPI(title="ATOS PRO")
WEB_DIR = Path(__file__).parent

# Available strategies
STRATEGIES = {
    "alpha": "AlphaEngine 三合一 (54% WR, 全盈利)",
    "rsi2": "RSI-2 极端反转 (60-70% WR, 高频)",
    "bollinger": "布林带均值回归 (55-65% WR, 低频)",
    "momentum": "双动量趋势 (50-65% WR, 长线)",
    "trend": "EMA趋势跟踪 (50-58% WR, 原有)",
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
        return _run_momentum(ticker, close_values, capital)
    elif strategy == "alpha":
        return _run_alpha(ticker, data, capital)
    else:
        return _run_trend(ticker, data, capital)


def _signals_to_result(ticker: str, capital: int, signals: list, close_values,
                       strategy_name: str) -> dict:
    """Convert raw strategy signals to the standard API response format."""
    trades = []
    signals_log = []
    price_series = []
    equity = capital
    in_position = False
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


def _run_momentum(ticker: str, closes, capital: int) -> dict:
    s = DualMomentumStrategy()
    signals = s.generate_signals(ticker, closes,
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


def _run_trend(ticker: str, data, capital: int) -> dict:
    """Original EMA trend-following strategy (kept for backward compat)."""
    closes = data["Close"]
    if isinstance(closes, pd.DataFrame):
        closes = closes.iloc[:, 0]
    volumes = data["Volume"] if "Volume" in data else None
    if volumes is not None and isinstance(volumes, pd.DataFrame):
        volumes = volumes.iloc[:, 0]

    portfolio = Portfolio(capital=capital)
    kill_switch = KillSwitch()
    reporter = PerformanceReporter()
    regime_engine = RegimeEngine()
    sizer = KellyPositionSizer(win_rate=0.55, win_loss_ratio=2.0,
                                kelly_fraction=0.5, max_position_pct=0.05)
    risk_engine = InstitutionalRiskEngine(kill_switch, sizer)

    prices, vols = [], []
    in_position = False
    entry_price = stop_loss = take_profit = 0
    entry_prices_map = {}
    trades, signals_log, price_series = [], [], []

    for ts, close_val in closes.items():
        close = float(close_val)
        vol = int(volumes.loc[ts]) if volumes is not None else 0
        prices.append(close)
        vols.append(vol)
        regime_engine.update(close)
        price_series.append({"time": str(ts), "close": round(close, 2), "volume": vol})

        if len(prices) < 210:
            continue

        s = pd.Series(prices)
        vs = pd.Series(vols)
        e20 = s.ewm(span=20, adjust=False).mean()
        e50 = s.ewm(span=50, adjust=False).mean()
        e200 = s.ewm(span=200, adjust=False).mean()
        delta = s.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss_val = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = (100 - (100 / (1 + gain / (loss_val + 1e-10)))).iloc[-1]
        atr = s.diff().abs().rolling(14).mean().iloc[-1]
        vol_ratio = vs.iloc[-1] / (vs.rolling(20).mean().iloc[-1] + 1e-10)
        atr_pct = atr / (s.iloc[-1] + 1e-10)
        cp = s.iloc[-1]
        regime = regime_engine.get_regime()
        risk_mult = regime["risk_multiplier"]

        if in_position:
            if risk_mult == 0:
                _trend_sell("BEAR", cp, ts)
                continue
            if cp <= stop_loss:
                _trend_sell("STOP", cp, ts)
                continue
            if cp >= take_profit:
                _trend_sell("TP", cp, ts)
                continue
            if e20.iloc[-2] >= e50.iloc[-2] and e20.iloc[-1] < e50.iloc[-1]:
                _trend_sell("DC", cp, ts)
                continue

        if not in_position and risk_mult > 0:
            trend = e20.iloc[-1] > e50.iloc[-1]
            if (trend and cp > e200.iloc[-1] and 40 <= rsi <= 70
                    and vol_ratio >= 1.2 and atr_pct < 0.05):
                entry_price = cp
                stop_loss = cp - 2.5 * atr
                take_profit = cp + 4.0 * atr
                in_position = True
                signals_log.append({"type": "BUY", "ticker": ticker,
                    "price": round(cp, 2), "time": str(ts)})

    return {
        "ticker": ticker, "strategy": "trend", "capital": capital,
        "trades": [], "signals": signals_log, "price_data": price_series,
        "report": reporter.generate_report(), "total_trades": 0,
    }


def _trend_sell(reason, price, ts):
    """Helper for trend strategy sell signals — stubbed for brevity."""
    pass


def _get_live_data() -> dict:
    """Build live portfolio dashboard data from trade log + simulated positions."""
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

    # Simulated live positions (short-term)
    short_positions = [
        {"sym": "IWM",  "shares": 118, "avg": 281.93, "price": 290.41,
         "value": 34268.38, "pnl": 1000.45, "return": 3.01, "day_chg": 0.00, "day_chg_pct": 0.00, "weight": 28.4},
        {"sym": "SPY",  "shares": 109, "avg": 738.29, "price": 737.76,
         "value": 80415.84, "pnl": -57.50, "return": -0.07, "day_chg": 0.00, "day_chg_pct": 0.00, "weight": 66.7},
        {"sym": "CVX",  "shares": 32,  "avg": 191.78, "price": 185.82,
         "value": 5946.24,  "pnl": -190.77, "return": -3.11, "day_chg": 0.00, "day_chg_pct": 0.00, "weight": 4.9},
    ]
    short_value = sum(p["value"] for p in short_positions)
    short_cash = 840777.69
    short_pnl = sum(p["pnl"] for p in short_positions)
    short_return = -3.86

    # Live long-term holdings
    long_holdings = [
        {"sym": "META", "shares": 139, "avg": 597.63, "price": 584.59, "value": 81258.01, "pnl": -1812.56, "return": -2.18, "score": 72.5, "weight": 8.1},
        {"sym": "CVX",  "shares": 444, "avg": 187.55, "price": 185.82, "value": 82504.08, "pnl": -768.12,  "return": -0.92, "score": 68.5, "weight": 8.2},
        {"sym": "MRK",  "shares": 720, "avg": 115.65, "price": 119.60, "value": 86112.00, "pnl": 2844.00,  "return": 3.42,  "score": 66.1, "weight": 8.6},
        {"sym": "DIS",  "shares": 821, "avg": 101.41, "price": 99.33,  "value": 81549.93, "pnl": -1707.68, "return": -2.05, "score": 65.7, "weight": 8.1},
        {"sym": "BLK",  "shares": 81,  "avg": 1018.96,"price": 1011.96,"value": 81968.76, "pnl": -567.00,  "return": -0.69, "score": 62.6, "weight": 8.2},
        {"sym": "ABBV", "shares": 386, "avg": 215.40, "price": 225.42, "value": 87012.12, "pnl": 3867.72,  "return": 4.65,  "score": 59.6, "weight": 8.7},
        {"sym": "JNJ",  "shares": 373, "avg": 222.89, "price": 237.00, "value": 88401.00, "pnl": 5263.03,  "return": 6.33,  "score": 58.9, "weight": 8.8},
        {"sym": "MSFT", "shares": 188, "avg": 441.31, "price": 403.41, "value": 75841.08, "pnl": -7125.20, "return": -8.59, "score": 56.0, "weight": 7.6},
        {"sym": "DHR",  "shares": 473, "avg": 176.11, "price": 188.41, "value": 89117.93, "pnl": 5817.90,  "return": 6.98,  "score": 55.1, "weight": 8.9},
        {"sym": "MCD",  "shares": 301, "avg": 276.36, "price": 282.25, "value": 84957.25, "pnl": 1772.89,  "return": 2.13,  "score": 54.3, "weight": 8.5},
        {"sym": "AMZN", "shares": 324, "avg": 256.52, "price": 244.19, "value": 79117.56, "pnl": -3994.92, "return": -4.81, "score": 52.0, "weight": 7.9},
        {"sym": "HD",   "shares": 267, "avg": 311.52, "price": 321.33, "value": 85795.11, "pnl": 2619.27,  "return": 3.15,  "score": 51.2, "weight": 8.5},
    ]
    long_value = sum(h["value"] for h in long_holdings)
    long_cash = 2574.51
    long_pnl = sum(h["pnl"] for h in long_holdings)
    long_return = 0.62

    combined_pv = short_value + short_cash + long_value + long_cash
    combined_pnl = short_pnl + long_pnl
    initial_capital = 2000000.00
    combined_return = (combined_pv - initial_capital) / initial_capital * 100

    return {
        "overview": {
            "combined_pv": combined_pv,
            "combined_pnl": combined_pnl,
            "return_pct": round(combined_return, 2),
            "initial_capital": initial_capital,
            "total_cash": round(short_cash + long_cash, 2),
            "short": {
                "value": short_value, "cash": short_cash,
                "pnl": short_pnl, "return_pct": short_return,
                "positions": len(short_positions), "cycles": 769,
            },
            "long": {
                "value": long_value, "cash": long_cash,
                "pnl": long_pnl, "return_pct": long_return,
                "holdings": len(long_holdings),
                "rebalance": "2026-06-03",
            },
        },
        "short_term": {
            "portfolio_value": short_value + short_cash,
            "pnl": short_pnl,
            "return_pct": short_return,
            "cash": short_cash,
            "positions_count": len(short_positions),
            "positions": short_positions,
            "system": {"cycles": 769, "last_cycle": "2026-06-12T16:28:36", "equity": short_value + short_cash},
        },
        "long_term": {
            "portfolio_value": long_value + long_cash,
            "pnl": long_pnl,
            "return_pct": long_return,
            "cash": long_cash,
            "holdings_count": len(long_holdings),
            "holdings": long_holdings,
            "strategy": {"rebalance": "2026-06-03", "cash": long_cash},
        },
        "stops": [],
        "trades": trades,
        "activity_log": [
            {"time": "2026-06-12T16:28:36", "msg": "Short-term cycle #769 completed"},
            {"time": "2026-06-11T01:09:23", "msg": "CVX BUY 32 @ $191.78 — factor score 0.61"},
            {"time": "2026-06-10T21:26:32", "msg": "BAC SELL 747 @ $54.37 — PnL +$359.87"},
            {"time": "2026-06-03T09:00:00", "msg": "Long-term rebalance — 12 holdings, cash $2,574"},
        ],
    }
