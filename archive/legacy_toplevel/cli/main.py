import typer
import asyncio
from rich.console import Console
from atos.infrastructure.event_bus import AsyncEventBus
from atos.infrastructure.events import MarketEvent, SignalEvent
from atos.market.regime.regime_engine import RegimeEngine
from atos.strategy.institutional_strategy import InstitutionalStrategy
from atos.risk.kelly_position_sizer import KellyPositionSizer
from atos.risk.institutional_risk_engine import InstitutionalRiskEngine
from atos.portfolio.portfolio import Portfolio
from atos.broker.futu.gateway import FutuGateway
from atos.backtest.backtest_engine import BacktestEngine
from atos.monitoring.kill_switch import KillSwitch
from atos.reporting.performance_report import PerformanceReporter

console = Console()
app = typer.Typer()

@app.command()
def run(ticker: str = "NVDA"):
    asyncio.run(main_runtime(ticker))

async def main_runtime(ticker: str):
    console.print("[bold green]ATOS PRO — 机构级策略启动[/bold green]")

    gateway = FutuGateway()
    gateway.connect()

    event_bus = AsyncEventBus()
    portfolio = Portfolio(capital=100000)
    kill_switch = KillSwitch()
    reporter = PerformanceReporter()
    regime_engine = RegimeEngine()
    sizer = KellyPositionSizer(win_rate=0.55, win_loss_ratio=2.0,
                                kelly_fraction=0.5, max_position_pct=0.05)
    risk_engine = InstitutionalRiskEngine(kill_switch, sizer)
    strategy = InstitutionalStrategy(event_bus, regime_engine)

    entry_prices = {}

    async def on_signal(signal: SignalEvent):
        try:
            kill_switch.check()
        except Exception:
            console.print("[red]Kill Switch 已触发，停止交易[/red]")
            return

        equity = portfolio.equity({ticker: signal.price})
        reporter.record_equity(equity)
        regime = regime_engine.get_regime()

        qty = risk_engine.approve_signal(
            equity=equity, price=signal.price,
            confidence=signal.confidence,
            regime_multiplier=regime["risk_multiplier"]
        )
        if qty <= 0:
            return

        ret, data = gateway.place_order(
            ticker=f"US.{ticker}", qty=qty,
            side=signal.side, simulate=True
        )
        if ret == 0:
            order_id = data.iloc[0]["order_id"]
            portfolio.update_fill(ticker, signal.side, qty, signal.price)
            if signal.side == "BUY":
                entry_prices[ticker] = signal.price
                console.print(
                    f"[green]✅ BUY {qty}股 {ticker} @ {signal.price:.2f} "
                    f"| Regime:{regime['regime']} | order:{order_id}[/green]"
                )
            else:
                entry = entry_prices.get(ticker, signal.price)
                pnl = (signal.price - entry) * qty
                reporter.record_trade(ticker, signal.side, qty, signal.price, pnl)
                console.print(f"[yellow]📤 SELL {qty}股 {ticker} @ {signal.price:.2f} | PnL: ${pnl:.2f}[/yellow]")
        else:
            console.print(f"[red]❌ 下单失败: {data}[/red]")

    event_bus.subscribe(MarketEvent, strategy.on_market)
    event_bus.subscribe(SignalEvent, on_signal)
    asyncio.create_task(event_bus.run())

    backtest = BacktestEngine(ticker=ticker, event_bus=event_bus)
    await backtest.run()

    console.print(reporter.generate_report())
    gateway.close()
