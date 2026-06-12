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

app = FastAPI(title="ATOS PRO")
WEB_DIR = Path(__file__).parent


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB_DIR / "templates" / "index.html").read_text()


@app.post("/api/run")
async def run_strategy(request: Request):
    body = await request.json()
    ticker = body.get("ticker", "NVDA").upper().strip()
    capital = body.get("capital", 100000)

    # run backtest in thread to avoid blocking event loop
    result = {}
    done = threading.Event()

    def _run():
        nonlocal result
        result = _sync_backtest(ticker, capital)
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    done.wait(timeout=120)
    return JSONResponse(result)


def _sync_backtest(ticker: str, capital: int) -> dict:
    """Synchronous backtest using the strategy logic directly."""
    # Download data
    try:
        data = yf.download(ticker, period="2y", progress=False)
    except Exception as e:
        return {"error": f"yfinance download failed: {e}", "ticker": ticker}

    if data is None or data.empty:
        return {"error": f"No data for {ticker}", "ticker": ticker}

    closes = data["Close"]
    if isinstance(closes, pd.DataFrame):
        closes = closes.iloc[:, 0]
    volumes = data["Volume"] if "Volume" in data else None
    if volumes is not None and isinstance(volumes, pd.DataFrame):
        volumes = volumes.iloc[:, 0]

    # Setup
    portfolio = Portfolio(capital=capital)
    kill_switch = KillSwitch()
    reporter = PerformanceReporter()
    regime_engine = RegimeEngine()
    sizer = KellyPositionSizer(
        win_rate=0.55, win_loss_ratio=2.0,
        kelly_fraction=0.5, max_position_pct=0.05
    )
    risk_engine = InstitutionalRiskEngine(kill_switch, sizer)

    # Strategy state (inlined from InstitutionalStrategy)
    prices = []
    vols = []
    in_position = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_prices_map = {}

    trades = []
    signals_log = []
    price_series = []

    def _ema(series, period):
        return series.ewm(span=period, adjust=False).mean()

    def _rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / (loss + 1e-10)
        return (100 - (100 / (1 + rs))).iloc[-1]

    def _atr(series, period=14):
        return series.diff().abs().rolling(period).mean().iloc[-1]

    def _execute_signal(side: str, confidence: float, curr_price: float, ts):
        """Execute a trade signal through risk engine and portfolio."""
        nonlocal in_position, entry_price, stop_loss, take_profit

        try:
            kill_switch.check()
        except Exception:
            signals_log.append({"type": "KILL_SWITCH", "time": str(ts)})
            return

        equity = portfolio.equity({ticker: curr_price})
        reporter.record_equity(equity)
        regime = regime_engine.get_regime()

        qty = risk_engine.approve_signal(
            equity=equity, price=curr_price,
            confidence=confidence,
            regime_multiplier=regime["risk_multiplier"]
        )
        if qty <= 0:
            return

        if side == "BUY":
            cost = qty * curr_price
            if portfolio.cash >= cost:
                portfolio.update_fill(ticker, side, qty, curr_price)
                entry_prices_map[ticker] = curr_price
                signals_log.append({
                    "type": "BUY", "ticker": ticker, "qty": qty,
                    "price": round(curr_price, 2),
                    "regime": regime["regime"],
                    "time": str(ts)
                })
        else:
            current = portfolio.positions.get(ticker, 0)
            if current >= qty:
                portfolio.update_fill(ticker, side, qty, curr_price)
                entry = entry_prices_map.get(ticker, curr_price)
                pnl = (curr_price - entry) * qty
                reporter.record_trade(ticker, side, qty, curr_price, pnl)
                trades.append({
                    "ticker": ticker, "side": side, "qty": qty,
                    "price": round(curr_price, 2), "pnl": round(pnl, 2),
                    "time": str(ts)
                })
                signals_log.append({
                    "type": "SELL", "ticker": ticker, "qty": qty,
                    "price": round(curr_price, 2), "pnl": round(pnl, 2),
                    "time": str(ts)
                })
            # always clear position state on sell attempt
            in_position = False

    # Main loop: iterate through each bar
    for ts, close_val in closes.items():
        close = float(close_val)
        vol = int(volumes.loc[ts]) if volumes is not None else 0

        prices.append(close)
        vols.append(vol)
        regime_engine.update(close)

        price_series.append({
            "time": str(ts),
            "close": round(close, 2),
            "volume": vol
        })

        if len(prices) < 210:
            continue

        closes_series = pd.Series(prices)
        vols_series = pd.Series(vols)

        ema20 = _ema(closes_series, 20)
        ema50 = _ema(closes_series, 50)
        ema200 = _ema(closes_series, 200)
        rsi = _rsi(closes_series)
        atr = _atr(closes_series)
        vol_ratio = vols_series.iloc[-1] / (vols_series.rolling(20).mean().iloc[-1] + 1e-10)
        atr_pct = atr / (closes_series.iloc[-1] + 1e-10)

        curr_price = closes_series.iloc[-1]
        curr_ema20 = ema20.iloc[-1]
        curr_ema50 = ema50.iloc[-1]
        curr_ema200 = ema200.iloc[-1]
        prev_ema20 = ema20.iloc[-2]
        prev_ema50 = ema50.iloc[-2]

        regime = regime_engine.get_regime()
        risk_mult = regime["risk_multiplier"]

        # Bear market forced liquidation
        if risk_mult == 0.0 and in_position:
            _execute_signal("SELL", 1.0, curr_price, ts)
            continue

        # Stop loss
        if in_position and curr_price <= stop_loss:
            _execute_signal("SELL", 1.0, curr_price, ts)
            continue

        # Take profit
        if in_position and curr_price >= take_profit:
            _execute_signal("SELL", 0.9, curr_price, ts)
            continue

        # Death cross exit
        if in_position and prev_ema20 >= prev_ema50 and curr_ema20 < curr_ema50:
            _execute_signal("SELL", 0.8, curr_price, ts)
            continue

        # Five-condition entry（优化版）
        if not in_position and risk_mult > 0:
            trend_up = curr_ema20 > curr_ema50
            above_ema200 = curr_price > curr_ema200
            rsi_ok = 40 <= rsi <= 70
            volume_ok = vol_ratio >= 1.2
            volatility_ok = atr_pct < 0.05

            if trend_up and above_ema200 and rsi_ok and volume_ok and volatility_ok:
                entry_price = curr_price
                stop_loss = curr_price - 2.5 * atr
                take_profit = curr_price + 4.0 * atr
                in_position = True
                _execute_signal("BUY", 0.9 * risk_mult, curr_price, ts)

    report = reporter.generate_report()

    return {
        "ticker": ticker,
        "capital": capital,
        "trades": trades,
        "signals": signals_log,
        "price_data": price_series,
        "report": report,
        "total_trades": len(trades),
    }
