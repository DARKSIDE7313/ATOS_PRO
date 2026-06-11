"""
ATOS PRO v2 — Phoenix 凤凰长线策略主运行器
==============================================
整合三层策略 + 风险控制 + 现金管理 + 订单执行。

调度频率：
  Layer 1 (基础层):  每 15 天 — 股息贵族选股 + 增强定投
  Layer 2 (核心层):  每 91 天 — 多因子质量组合再平衡
  Layer 3 (战术层):  每 30 天 — 因子轮动 + 行业轮动 + 内部人追踪
  风险监控:          每天      — 回撤/集中度/流动性检查
  现金管理(回撤检查): 每小时    — 触发时自动抄底
  月报:              每月      — 自动生成投资报告
"""

import os, sys, json, datetime, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yfinance as yf
from atos.core.logging import get_logger
from atos.longterm.config import CAPITAL, LAYER1, LAYER2, LAYER3, RISK, SCHEDULE
from atos.longterm.market_thermometer import MarketThermometer
from atos.longterm.cash_manager import get_cash_manager, should_buy_the_dip
from atos.longterm.layer1_foundation import get_layer1, run_layer1
from atos.longterm.layer3_tactical import get_layer3, run_layer3
from atos.longterm.risk_monitor import get_risk_monitor, full_risk_check
from atos.longterm.tactical_overlay import get_overlay, apply_tactical_overlay

logger = get_logger("phoenix.runner")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORT_DIR = os.path.join(BASE, "reports")
STATE_FILE = os.path.join(BASE, "phoenix_state.json")


class PhoenixRunner:

    def __init__(self):
        self.layer1 = get_layer1()
        self.layer3 = get_layer3()
        self.risk = get_risk_monitor()
        self.cash = get_cash_manager()
        self.thermometer = MarketThermometer()
        self.state = self.load_state()
        self.previous_market_phase = self.state.get("market_phase", "NEUTRAL")
        self._pending_orders_file = os.path.join(
            os.path.dirname(STATE_FILE), "state", "phoenix_orders.json"
        )

    def _load_pending_orders(self) -> set:
        """加载已提交的订单签名（去重用）"""
        try:
            with open(self._pending_orders_file) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_pending_orders(self, signatures: set):
        """保存订单签名"""
        try:
            os.makedirs(os.path.dirname(self._pending_orders_file), exist_ok=True)
            with open(self._pending_orders_file, "w") as f:
                json.dump(list(signatures)[-500:], f)  # 保留最近500条
        except Exception:
            pass

    def _sign_order(self, order: dict) -> str:
        """生成订单唯一签名: LAYER:SYMBOL:ACTION:ROUND"""
        return f"{order.get('layer','')}:{order.get('symbol','')}:{order.get('action','')}:{order.get('quantity',0)}"

    def _deduplicate_orders(self, orders: list[dict]) -> list[dict]:
        """去除已提交过的重复订单"""
        submitted = self._load_pending_orders()
        fresh = []
        new_sigs = set()
        for o in orders:
            sig = self._sign_order(o)
            if sig in submitted:
                logger.debug(f"跳过重复订单: {sig}")
                continue
            fresh.append(o)
            new_sigs.add(sig)
        if new_sigs:
            self._save_pending_orders(submitted | new_sigs)
        if len(fresh) < len(orders):
            logger.info(f"去重: {len(orders)} → {len(fresh)} 个新订单")
        return fresh

    def load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "version": "2.0",
            "created": datetime.datetime.now().isoformat(),
            "runs": 0,
            "layer1_last_run": None,
            "layer2_last_run": None,
            "layer3_last_run": None,
            "risk_last_check": None,
            "market_phase": "NEUTRAL",
            "deployed_cash": 0.0,
        }

    def save_state(self):
        self.state["last_saved"] = datetime.datetime.now().isoformat()
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def should_run_layer(self, layer: str) -> bool:
        key = f"{layer}_last_run"
        last = self.state.get(key)
        if last is None:
            return True
        try:
            last_dt = datetime.datetime.fromisoformat(last)
        except Exception:
            return True
        intervals = {
            "layer1": SCHEDULE.get("layer1_interval_minutes", 21600),
            "layer2": SCHEDULE.get("layer2_interval_minutes", 131040),
            "layer3": SCHEDULE.get("layer3_interval_minutes", 43200),
            "risk": SCHEDULE.get("risk_check_interval_minutes", 1440),
        }
        interval = intervals.get(layer, 1440)
        elapsed = (datetime.datetime.now() - last_dt).total_seconds() / 60
        return elapsed >= interval

    def is_market_open(self) -> bool:
        try:
            from atos.live.futu_bridge import is_market_open as f_is_open
            return f_is_open()[0]
        except Exception:
            import datetime as dt
            now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))
            if now.weekday() >= 5:
                return False
            market_open = dt.time(9, 30)
            market_close = dt.time(16, 0)
            return market_open <= now.time() <= market_close

    def execute_orders(self, orders: list[dict], dry_run: bool = True) -> dict:
        if not orders:
            return {"executed": 0, "skipped": 0, "dry_run": dry_run}
        logger.info(f"执行 {len(orders)} 个订单 (dry_run={dry_run})...")
        results = {"executed": 0, "skipped": 0, "dry_run": dry_run, "details": []}
        for order in orders:
            sym = order.get("symbol", "UNKNOWN")
            qty = order.get("quantity", 0)
            action = order.get("action", "BUY")
            price = order.get("price", 0)
            if qty <= 0:
                results["skipped"] += 1
                results["details"].append({"symbol": sym, "status": "SKIPPED", "reason": f"qty={qty}"})
                continue
            if dry_run:
                logger.info(f"  [DRY RUN] {action} {sym} x{qty} @ ${price:.2f}")
                results["executed"] += 1
                results["details"].append({
                    "symbol": sym, "status": "DRY_RUN",
                    "action": action, "quantity": qty, "price": price,
                })
            else:
                try:
                    from atos.live.futu_bridge import place_order
                    exec_result = place_order(symbol=sym, action=action, quantity=qty, price=price)
                    logger.info(f"  [REAL] {action} {sym} x{qty} — {exec_result}")
                    results["executed"] += 1
                    results["details"].append(exec_result)
                except ImportError:
                    logger.warning("futu_bridge 不可用，跳过真实下单")
                    results["skipped"] += 1
                    results["details"].append({"symbol": sym, "status": "BRIDGE_UNAVAILABLE"})
                except Exception as e:
                    logger.error(f"下单失败 {sym}: {e}")
                    results["skipped"] += 1
                    results["details"].append({"symbol": sym, "status": "ERROR", "error": str(e)})
        logger.info(f"订单执行完成: {results['executed']}/{len(orders)} 成功")
        return results

    def run_layer2(self) -> dict:
        """运行 Layer 2 核心层排...名，返回排名结果"""
        try:
            from atos.longterm.engine import comprehensive_long_term_rank, build_long_term_portfolio
            from atos.core.universe import ALL_SYMBOLS
            ranking = comprehensive_long_term_rank(ALL_SYMBOLS)
            top_n = LAYER2.get("multifactor_top_n", 25)
            portfolio = build_long_term_portfolio(ranking, capital=CAPITAL["total"] * CAPITAL["layer2_pct"],
                                                  max_positions=top_n)
            positions = portfolio.get("positions", [])
            # 缓存结果，供 get_orders() 使用
            self._last_layer2_ranking = ranking
            self._last_layer2_portfolio = portfolio
            return {
                "layer": "core",
                "timestamp": datetime.datetime.now().isoformat(),
                "total_capital": CAPITAL["total"] * CAPITAL["layer2_pct"],
                "rankings": len(ranking),
                "selected": len(positions),
                "top_5": [p["symbol"] for p in positions[:5]],
            }
        except Exception as e:
            logger.error(f"Layer2 执行失败: {e}")
            return {"layer": "core", "error": str(e)}

    def get_layer2_orders(self) -> list[dict]:
        """从 Layer 2 排名中生成买入订单（使用缓存）"""
        orders = []
        portfolio = getattr(self, '_last_layer2_portfolio', None)
        if not portfolio or not portfolio.get("positions"):
            return orders
        l2_capital = CAPITAL["total"] * CAPITAL["layer2_pct"]
        positions = portfolio["positions"]
        weight_per = portfolio.get("weight_per_position", 1.0 / max(len(positions), 1))
        for p in positions:
            try:
                info = yf.Ticker(p["symbol"]).info or {}
                price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or 0)
                if price > 0:
                    capital = l2_capital * weight_per
                    orders.append({
                        "layer": "core", "symbol": p["symbol"],
                        "action": "BUY", "quantity": max(1, int(capital / price)),
                        "price": round(price, 2),
                        "reason": f"多因子排名 #{p.get('composite_score', 0):.0f}分",
                    })
            except Exception as e:
                logger.warning(f"L2 订单 {p['symbol']}: {e}")
        return orders

    def full_run(self) -> dict:
        start_time = time.time()
        logger.info("🔥 Phoenix 长线策略启动")
        results = {}
        all_actions = []
        all_orders = []

        # Step 1: 市场温度
        thermo = self.thermometer.comprehensive_score()
        self.state["market_phase"] = thermo["phase"]
        if thermo["phase"] != self.previous_market_phase:
            logger.info(f"⚠️ 市场阶段变化: {self.previous_market_phase} → {thermo['phase']}")
            self.previous_market_phase = thermo["phase"]
        results["thermometer"] = thermo

        # Step 2: 风险检查
        if self.should_run_layer("risk"):
            risk_result = self.risk.full_check(positions={}, current_value=CAPITAL["total"])
            results["risk_check"] = risk_result
            if risk_result["actions_required"]:
                logger.warning(f"⚠️ 风险检查发现 {len(risk_result['actions_required'])} 个问题")
                all_actions.extend(risk_result["actions_required"])
            self.state["risk_last_check"] = datetime.datetime.now().isoformat()
        else:
            results["risk_check"] = {"skipped": True}

        # Step 3: 现金部署检查
        dip_result = should_buy_the_dip()
        results["dip_check"] = dip_result
        if dip_result.get("deploy"):
            logger.info(f"💵 触发抄底: {dip_result['reason']}")
            all_actions.append({"type": "deploy_cash", "pct": dip_result["pct"], "reason": dip_result["reason"]})

        # Step 4: 各层策略
        if self.should_run_layer("layer1"):
            l1_result = run_layer1()
            results["layer1"] = l1_result
            try:
                l1_orders = self.layer1.get_orders()
                all_orders.extend(l1_orders)
            except Exception as e:
                logger.warning(f"L1 订单: {e}")
            self.state["layer1_last_run"] = datetime.datetime.now().isoformat()

        if self.should_run_layer("layer2"):
            l2_result = self.run_layer2()
            results["layer2"] = l2_result
            try:
                l2_orders = self.get_layer2_orders()
                all_orders.extend(l2_orders)
                logger.info(f"  Layer 2 生成 {len(l2_orders)} 个订单")
            except Exception as e:
                logger.warning(f"L2 订单: {e}")
            self.state["layer2_last_run"] = datetime.datetime.now().isoformat()

        if self.should_run_layer("layer3"):
            l3_result = run_layer3()
            results["layer3"] = l3_result
            try:
                l3_orders = self.layer3.get_orders()
                all_orders.extend(l3_orders)
            except Exception as e:
                logger.warning(f"L3 订单: {e}")
            self.state["layer3_last_run"] = datetime.datetime.now().isoformat()

        # Step 5: Tactical Overlay（去重→机制调整→统计过滤）
        if all_orders:
            overlay = get_overlay()
            
            # 先调仓位大小（便宜操作）
            all_orders = overlay.adjust_for_regime(all_orders)
            
            # 先去重（避免对重复订单做昂贵API调用）
            all_orders = self._deduplicate_orders(all_orders)
            
            if all_orders:
                # 再统计过滤（昂贵API调用，但现在只对唯一订单）
                all_orders, screen_report = overlay.screen_orders(all_orders)
                results["tactical_screen"] = screen_report
            
            if all_orders and self.is_market_open():
                exec_result = self.execute_orders(all_orders, dry_run=True)
                results["order_execution"] = exec_result
            else:
                results["order_execution"] = {"executed": 0, "reason": "market_closed"}
        else:
            results["order_execution"] = {"executed": 0, "reason": "no_orders"}

        # Step 6: 汇总
        self.state["runs"] += 1
        self.state["last_full_run"] = datetime.datetime.now().isoformat()
        self.save_state()

        elapsed = time.time() - start_time
        summary = {
            "run_id": self.state["runs"],
            "timestamp": datetime.datetime.now().isoformat(),
            "market_phase": thermo["phase"],
            "thermo_score": thermo["score"],
            "total_orders": len(all_orders),
            "total_actions": len(all_actions),
            "actions": all_actions,
            "elapsed_seconds": round(elapsed, 1),
            "completed": True,
        }
        results["summary"] = summary
        self.save_report(results)

        logger.info(f"🎉 Phoenix 完成 (#{summary['run_id']}) | 市场 {thermo['phase']} "
                    f"| {len(all_orders)} 订单 | {elapsed:.1f}s")
        return results

    def save_report(self, results: dict):
        os.makedirs(REPORT_DIR, exist_ok=True)
        filename = f"phoenix_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(REPORT_DIR, filename)
        with open(filepath, "w") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"📄 报告: {filepath}")

    def status(self) -> dict:
        thermo = self.thermometer.comprehensive_score()
        dip = should_buy_the_dip()
        return {
            "state": self.state,
            "thermometer": thermo,
            "dip_status": dip,
            "capital_allocation": {
                "layer1": CAPITAL["total"] * CAPITAL["layer1_pct"],
                "layer2": CAPITAL["total"] * CAPITAL["layer2_pct"],
                "layer3": CAPITAL["total"] * CAPITAL["layer3_pct"],
                "cash_reserve": CAPITAL["total"] * CAPITAL.get("cash_reserve_pct", 0.05),
            },
        }


_phoenix_instance: PhoenixRunner = None

def get_phoenix() -> PhoenixRunner:
    global _phoenix_instance
    if _phoenix_instance is None:
        _phoenix_instance = PhoenixRunner()
    return _phoenix_instance

def run_phoenix() -> dict:
    return get_phoenix().full_run()

def quick_status() -> dict:
    return get_phoenix().status()


if __name__ == "__main__":
    runner = PhoenixRunner()
    import argparse
    parser = argparse.ArgumentParser(description="Phoenix 凤凰长线策略")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(runner.status(), indent=2, default=str))
    elif args.report:
        result = runner.status()
        runner.save_report(result)
        print(json.dumps(result, indent=2, default=str))
    else:
        results = runner.full_run()
        summary = results.get("summary", {})
        print(f"\nPhoenix 完成 | 第 {summary.get('run_id', 1)} 次")
        print(f"市场: {summary.get('market_phase')} | 温度: {summary.get('thermo_score')}")
