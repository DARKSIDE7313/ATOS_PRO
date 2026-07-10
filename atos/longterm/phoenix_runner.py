"""
ATOS PRO v3 — Phoenix 凤凰长线策略主运行器
==============================================
整合三层策略 + 卖出逻辑 + 仓位跟踪 + 风险控制 + 现金管理 + 订单执行。

调度频率 (v10 加速):
  Layer 1 (基础层):  每 7 天 — 股息贵族选股 + 增强定投
  Layer 2 (核心层):  每 30 天 — 多因子质量组合再平衡
  Layer 3 (战术层):  每 15 天 — 因子轮动 + 行业轮动 + 内部人追踪
  卖出检查:          每次运行 — 自动检查各层卖出触发 + 止损
  风险监控:          每天      — 回撤/集中度/流动性检查
  现金管理(回撤检查): 每小时    — 触发时自动抄底
  月报:              每月      — 自动生成投资报告

用法:
  python -m atos.longterm.phoenix_runner --run        # dry run（默认）
  python -m atos.longterm.phoenix_runner --run --live  # 实盘交易
  python -m atos.longterm.phoenix_runner --status      # 查看状态
"""

import os, sys, json, datetime, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yfinance as yf
from atos.core.logging import get_logger
from atos.longterm.config import CAPITAL, LAYER1, LAYER2, LAYER3, RISK, SCHEDULE
from atos.longterm.market_thermometer import MarketThermometer
from atos.longterm.cash_manager import get_cash_manager, should_buy_the_dip
from atos.longterm.layer1_foundation import get_layer1, run_layer1
from atos.longterm.layer2_core import get_layer2, run_layer2
from atos.longterm.layer3_tactical import get_layer3, run_layer3
from atos.longterm.risk_monitor import get_risk_monitor, full_risk_check
from atos.longterm.tactical_overlay import get_overlay, apply_tactical_overlay

logger = get_logger("phoenix.runner")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORT_DIR = os.path.join(BASE, "reports")
STATE_FILE = os.path.join(BASE, "phoenix_state.json")


class PhoenixRunner:
    """
    Phoenix 凤凰长线策略主运行器 v3。

    新增 (v3):
      - 完整仓位跟踪（phoenix_state.json 持久化）
      - 各层卖出逻辑（sell before buy）
      - Layer 2 独立类（layer2_core.py）
      - --live 实盘开关
      - 组合估值 + 盈亏计算
    """

    def __init__(self):
        self.layer1 = get_layer1()
        self.layer2 = get_layer2()
        self.layer3 = get_layer3()
        self.risk = get_risk_monitor()
        self.cash = get_cash_manager()
        self.thermometer = MarketThermometer()
        self.state = self.load_state()
        self.previous_market_phase = self.state.get("market_phase", "NEUTRAL")
        self._pending_orders_file = os.path.join(
            os.path.dirname(STATE_FILE), "state", "phoenix_orders.json"
        )

    # ═══════════════════════════════════════════
    # 状态管理
    # ═══════════════════════════════════════════

    def load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "version": "3.0",
            "created": datetime.datetime.now().isoformat(),
            "runs": 0,
            "layer1_last_run": None,
            "layer2_last_run": None,
            "layer3_last_run": None,
            "risk_last_check": None,
            "market_phase": "NEUTRAL",
            "deployed_cash": 0.0,
            "positions": {},        # {symbol: {layer, shares, avg_cost, buy_date, sub}}
            "cash": CAPITAL["total"],
            "total_deposited": CAPITAL["total"],
            "trade_history": [],     # 最近 100 笔交易记录
        }

    def save_state(self):
        self.state["last_saved"] = datetime.datetime.now().isoformat()
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def get_positions(self) -> dict:
        """获取当前所有持仓"""
        return self.state.get("positions", {})

    def update_position(self, symbol: str, layer: str, shares: int,
                        price: float, action: str, sub: str = ""):
        """更新单只持仓"""
        positions = self.state.setdefault("positions", {})

        if action == "BUY":
            if symbol in positions:
                # 加仓：更新平均成本
                old = positions[symbol]
                old_cost = old.get("avg_cost", 0)
                old_shares = old.get("shares", 0)
                new_total_cost = old_cost * old_shares + price * shares
                new_total_shares = old_shares + shares
                positions[symbol] = {
                    "layer": layer,
                    "sub": sub,
                    "shares": new_total_shares,
                    "avg_cost": round(new_total_cost / new_total_shares, 2),
                    "buy_date": old.get("buy_date", datetime.date.today().isoformat()),
                }
            else:
                positions[symbol] = {
                    "layer": layer,
                    "sub": sub,
                    "shares": shares,
                    "avg_cost": round(price, 2),
                    "buy_date": datetime.date.today().isoformat(),
                }
            self.state["cash"] = self.state.get("cash", 0) - shares * price

        elif action == "SELL":
            if symbol in positions:
                old = positions[symbol]
                remaining = old.get("shares", 0) - shares
                if remaining <= 0:
                    del positions[symbol]
                else:
                    positions[symbol]["shares"] = remaining
                self.state["cash"] = self.state.get("cash", 0) + shares * price

        # 记录交易历史
        history = self.state.setdefault("trade_history", [])
        history.append({
            "date": datetime.datetime.now().isoformat(),
            "symbol": symbol,
            "layer": layer,
            "action": action,
            "shares": shares,
            "price": round(price, 2),
            "value": round(shares * price, 2),
        })
        # 只保留最近 200 条
        self.state["trade_history"] = history[-200:]

    def get_portfolio_value(self) -> float:
        """计算当前组合总市值（现金 + 持仓）"""
        positions_value = 0.0
        for symbol, pos in self.get_positions().items():
            try:
                stock = yf.Ticker(symbol)
                info = stock.info or {}
                price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
            except Exception:
                price = pos.get("avg_cost", 0)
            positions_value += pos.get("shares", 0) * price
        return self.state.get("cash", 0) + positions_value

    def get_pnl(self) -> dict:
        """计算组合总盈亏"""
        total_cost = 0.0
        total_value = 0.0
        positions_detail = []
        for symbol, pos in self.get_positions().items():
            cost = pos.get("shares", 0) * pos.get("avg_cost", 0)
            try:
                stock = yf.Ticker(symbol)
                info = stock.info or {}
                price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or pos.get("avg_cost", 0))
            except Exception:
                price = pos.get("avg_cost", 0)
            value = pos.get("shares", 0) * price
            total_cost += cost
            total_value += value
            pnl_pct = (price - pos.get("avg_cost", 0)) / pos.get("avg_cost", 0) if pos.get("avg_cost", 0) > 0 else 0
            positions_detail.append({
                "symbol": symbol,
                "layer": pos.get("layer", ""),
                "shares": pos.get("shares", 0),
                "avg_cost": pos.get("avg_cost", 0),
                "current_price": round(price, 2),
                "market_value": round(value, 2),
                "pnl_pct": round(pnl_pct * 100, 2),
            })

        return {
            "total_cost": round(total_cost, 2),
            "total_value": round(total_value, 2),
            "total_pnl": round(total_value - total_cost, 2),
            "total_pnl_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
            "cash": round(self.state.get("cash", 0), 2),
            "positions": positions_detail,
        }

    # ═══════════════════════════════════════════
    # 订单去重
    # ═══════════════════════════════════════════

    def _load_pending_orders(self) -> set:
        try:
            with open(self._pending_orders_file) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_pending_orders(self, signatures: set):
        try:
            os.makedirs(os.path.dirname(self._pending_orders_file), exist_ok=True)
            with open(self._pending_orders_file, "w") as f:
                json.dump(list(signatures)[-500:], f)
        except Exception:
            pass

    def _sign_order(self, order: dict) -> str:
        return f"{order.get('layer','')}:{order.get('symbol','')}:{order.get('action','')}:{order.get('quantity',0)}"

    def _deduplicate_orders(self, orders: list[dict]) -> list[dict]:
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

    # ═══════════════════════════════════════════
    # 调度控制
    # ═══════════════════════════════════════════

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
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
            if now.weekday() >= 5:
                return False
            market_open = datetime.time(9, 30)
            market_close = datetime.time(16, 0)
            return market_open <= now.time() <= market_close

    # ═══════════════════════════════════════════
    # 订单执行
    # ═══════════════════════════════════════════

    def execute_orders(self, orders: list[dict], dry_run: bool = True) -> dict:
        """
        执行订单列表。先卖后买（释放现金）。

        Args:
            orders: 订单列表
            dry_run: True=演习, False=实盘
        """
        if not orders:
            return {"executed": 0, "skipped": 0, "dry_run": dry_run}

        # 卖单优先（释放现金）
        sells = [o for o in orders if o.get("action") == "SELL"]
        buys = [o for o in orders if o.get("action") == "BUY"]
        sorted_orders = sells + buys

        logger.info(f"执行 {len(sorted_orders)} 个订单 ({len(sells)}卖 {len(buys)}买, dry_run={dry_run})...")
        results = {"executed": 0, "skipped": 0, "dry_run": dry_run, "details": []}

        for order in sorted_orders:
            sym = order.get("symbol", "UNKNOWN")
            qty = order.get("quantity", 0)
            action = order.get("action", "BUY")
            price = order.get("price", 0)
            layer = order.get("layer", "")
            sub = order.get("sub", "")

            if qty <= 0:
                results["skipped"] += 1
                results["details"].append({"symbol": sym, "status": "SKIPPED", "reason": f"qty={qty}"})
                continue

            if dry_run:
                logger.info(f"  [DRY RUN] {action} {sym} x{qty} @ ${price:.2f} — {order.get('reason','')}")
                results["executed"] += 1
                results["details"].append({
                    "symbol": sym, "status": "DRY_RUN",
                    "action": action, "quantity": qty, "price": price,
                    "layer": layer, "reason": order.get("reason", ""),
                })
                # Dry run 也更新模拟持仓
                self.update_position(sym, layer, qty, price, action, sub)
            else:
                try:
                    from atos.live.futu_bridge import safe_place_order
                    side = "BUY" if action == "BUY" else "SELL"
                    exec_result = safe_place_order(ticker=sym, side=side, quantity=qty)
                    logger.info(f"  [REAL] {action} {sym} x{qty} — {exec_result}")
                    results["executed"] += 1
                    results["details"].append(exec_result)
                    self.update_position(sym, layer, qty, price, action, sub)
                except ImportError:
                    logger.warning("futu_bridge 不可用，跳过真实下单")
                    results["skipped"] += 1
                    results["details"].append({"symbol": sym, "status": "BRIDGE_UNAVAILABLE"})
                except Exception as e:
                    logger.error(f"下单失败 {sym}: {e}")
                    results["skipped"] += 1
                    results["details"].append({"symbol": sym, "status": "ERROR", "error": str(e)})

        logger.info(f"订单执行完成: {results['executed']}/{len(sorted_orders)} 成功")
        return results

    # ═══════════════════════════════════════════
    # v10: 长线止损 + 再平衡
    # ═══════════════════════════════════════════

    def _check_long_stops(self) -> list[dict]:
        """检查长线持仓是否需要止损。
        规则: 浮亏>15% 减半仓, >25% 全清仓
        """
        sell_orders = []
        for sym, pos in self.get_positions().items():
            try:
                stock = yf.Ticker(sym)
                info = stock.info or {}
                price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or 0)
            except Exception:
                price = 0
            if price <= 0:
                continue

            avg_cost = pos.get("avg_cost", 0)
            if avg_cost <= 0:
                continue
            pnl_pct = (price - avg_cost) / avg_cost
            qty = pos.get("shares", 0)

            if pnl_pct <= -0.25:  # 亏超 25% → 全清
                sell_orders.append({
                    "action": "SELL", "symbol": sym, "quantity": qty,
                    "price": round(price, 2), "layer": "stop_loss",
                    "reason": f"长线深亏止损 {pnl_pct:.1%} (全清)",
                })
                logger.warning(f"🛑 长线清仓: {sym} {pnl_pct:.1%}")
            elif pnl_pct <= -0.15:  # 亏超 15% → 减半
                half = max(1, qty // 2)
                sell_orders.append({
                    "action": "SELL", "symbol": sym, "quantity": half,
                    "price": round(price, 2), "layer": "stop_loss",
                    "reason": f"长线减半 {pnl_pct:.1%}",
                })
                logger.warning(f"🟠 长线减半: {sym} {pnl_pct:.1%}")
        return sell_orders

    def _check_rebalance(self) -> list[dict]:
        """检查是否需要再平衡（每30天,权重偏离>5%触发）"""
        last_rebalance = self.state.get("last_rebalance_date")
        if last_rebalance:
            try:
                last_dt = datetime.datetime.fromisoformat(last_rebalance)
                days_since = (datetime.datetime.now() - last_dt).days
                if days_since < 30:
                    return []
            except Exception:
                pass

        positions = self.get_positions()
        if len(positions) < 3:
            return []

        # 计算当前权重
        total_val = self.get_portfolio_value()
        if total_val <= 0:
            return []
        target_w = 1.0 / len(positions)
        orders = []
        for sym, pos in positions.items():
            try:
                stock = yf.Ticker(sym)
                info = stock.info or {}
                price = float(info.get("currentPrice", 0) or info.get("regularMarketPrice", 0) or 0)
            except Exception:
                price = 0
            if price <= 0:
                continue
            current_val = pos.get("shares", 0) * price
            current_w = current_val / total_val
            if abs(current_w - target_w) > 0.05:  # 偏离>5%
                delta_val = (target_w - current_w) * total_val
                qty = int(abs(delta_val) / price)
                if qty > 0:
                    action = "BUY" if delta_val > 0 else "SELL"
                    orders.append({
                        "action": action, "symbol": sym, "quantity": qty,
                        "price": round(price, 2), "layer": "rebalance",
                        "reason": f"再平衡: 当前{current_w:.1%}→目标{target_w:.1%}",
                    })

        if orders:
            self.state["last_rebalance_date"] = datetime.datetime.now().isoformat()
        return orders

    # ═══════════════════════════════════════════
    # 主运行流程
    # ═══════════════════════════════════════════

    def full_run(self, dry_run: bool = True) -> dict:
        """
        Phoenix 完整运行。

        Args:
            dry_run: False 启用实盘交易
        """
        start_time = time.time()
        mode = "DRY_RUN" if dry_run else "LIVE"
        logger.info(f"🔥 Phoenix 长线策略启动 [{mode}]")
        results = {}
        all_actions = []
        all_sell_orders = []
        all_buy_orders = []

        # 获取当前持仓
        current_positions = self.get_positions()

        # Step 1: 市场温度
        thermo = self.thermometer.comprehensive_score()
        self.state["market_phase"] = thermo["phase"]
        if thermo["phase"] != self.previous_market_phase:
            logger.info(f"⚠️ 市场阶段变化: {self.previous_market_phase} → {thermo['phase']}")
            self.previous_market_phase = thermo["phase"]
        results["thermometer"] = thermo

        # Step 2: 风险检查（用真实持仓和市值）
        if self.should_run_layer("risk"):
            portfolio_value = self.get_portfolio_value()
            risk_result = self.risk.full_check(
                positions=current_positions, current_value=portfolio_value
            )
            results["risk_check"] = risk_result
            if risk_result["actions_required"]:
                logger.warning(f"⚠️ 风险检查发现 {len(risk_result['actions_required'])} 个问题")
                all_actions.extend(risk_result["actions_required"])
            self.state["risk_last_check"] = datetime.datetime.now().isoformat()
        else:
            results["risk_check"] = {"skipped": True}

        # Step 3: 卖出检查（每次都跑，不是定时）
        # 卖单优先，因为要释放现金
        try:
            l1_sells = self.layer1.get_sell_orders(current_positions)
            all_sell_orders.extend(l1_sells)
        except Exception as e:
            logger.warning(f"L1 卖出检查: {e}")

        try:
            l2_sells = self.layer2.get_sell_orders(current_positions)
            all_sell_orders.extend(l2_sells)
        except Exception as e:
            logger.warning(f"L2 卖出检查: {e}")

        try:
            l3_sells = self.layer3.get_sell_orders(current_positions)
            all_sell_orders.extend(l3_sells)
        except Exception as e:
            logger.warning(f"L3 卖出检查: {e}")

        # Step 4: 长线止损检查 (v10 新增)
        long_stop_sells = self._check_long_stops()
        if long_stop_sells:
            logger.warning(f"🛑 长线止损触发: {len(long_stop_sells)} 只")
            all_sell_orders.extend(long_stop_sells)

        # Step 5: 再平衡检查 (v10 新增)
        rebalance_orders = self._check_rebalance()
        if rebalance_orders:
            logger.info(f"⚖️ 再平衡: {len(rebalance_orders)} 个订单")
            all_sell_orders.extend([o for o in rebalance_orders if o.get("action") == "SELL"])
            all_buy_orders.extend([o for o in rebalance_orders if o.get("action") == "BUY"])

        # Step 6: 现金部署检查
        dip_result = should_buy_the_dip()
        results["dip_check"] = dip_result
        if dip_result.get("deploy"):
            logger.info(f"💵 触发抄底: {dip_result['reason']}")
            all_actions.append({"type": "deploy_cash", "pct": dip_result["pct"], "reason": dip_result["reason"]})

        # Step 7: 各层策略（买入）
        if self.should_run_layer("layer1"):
            l1_result = run_layer1()
            results["layer1"] = l1_result
            try:
                l1_buys = self.layer1.get_buy_orders(current_positions)
                all_buy_orders.extend(l1_buys)
            except Exception as e:
                logger.warning(f"L1 买入: {e}")
            self.state["layer1_last_run"] = datetime.datetime.now().isoformat()

        if self.should_run_layer("layer2"):
            l2_result = run_layer2()
            results["layer2"] = l2_result
            try:
                l2_buys = self.layer2.get_buy_orders(current_positions)
                all_buy_orders.extend(l2_buys)
                logger.info(f"  Layer 2 生成 {len(l2_buys)} 个买入订单")
            except Exception as e:
                logger.warning(f"L2 买入: {e}")
            self.state["layer2_last_run"] = datetime.datetime.now().isoformat()

        if self.should_run_layer("layer3"):
            l3_result = run_layer3()
            results["layer3"] = l3_result
            try:
                l3_buys = self.layer3.get_buy_orders(current_positions)
                all_buy_orders.extend(l3_buys)
            except Exception as e:
                logger.warning(f"L3 买入: {e}")
            self.state["layer3_last_run"] = datetime.datetime.now().isoformat()

        # Step 8: Tactical Overlay（卖单+买单一起处理）
        all_orders = all_sell_orders + all_buy_orders  # 卖单在前
        if all_orders:
            overlay = get_overlay()
            all_orders = overlay.adjust_for_regime(all_orders)
            all_orders = self._deduplicate_orders(all_orders)

            if all_orders:
                all_orders, screen_report = overlay.screen_orders(all_orders)
                results["tactical_screen"] = screen_report

            if all_orders and (self.is_market_open() or dry_run):
                exec_result = self.execute_orders(all_orders, dry_run=dry_run)
                results["order_execution"] = exec_result
            else:
                results["order_execution"] = {
                    "executed": 0,
                    "reason": "market_closed" if not self.is_market_open() else "no_orders_after_filter"
                }
        else:
            results["order_execution"] = {"executed": 0, "reason": "no_orders"}

        # Step 7: 汇总
        self.state["runs"] += 1
        self.state["last_full_run"] = datetime.datetime.now().isoformat()
        self.state["portfolio_value"] = self.get_portfolio_value()
        self.save_state()

        elapsed = time.time() - start_time
        pnl = self.get_pnl()
        summary = {
            "run_id": self.state["runs"],
            "mode": mode,
            "timestamp": datetime.datetime.now().isoformat(),
            "market_phase": thermo["phase"],
            "thermo_score": thermo["score"],
            "sell_orders": len(all_sell_orders),
            "buy_orders": len(all_buy_orders),
            "total_orders": len(all_orders),
            "total_actions": len(all_actions),
            "actions": all_actions,
            "portfolio": pnl,
            "elapsed_seconds": round(elapsed, 1),
            "completed": True,
        }
        results["summary"] = summary
        self.save_report(results)

        logger.info(
            f"🎉 Phoenix 完成 (#{summary['run_id']}) [{mode}] | 市场 {thermo['phase']} "
            f"| {len(all_sell_orders)}卖 {len(all_buy_orders)}买 "
            f"| 组合 ${pnl['total_value']:,.0f} ({pnl['total_pnl_pct']:+.1f}%) "
            f"| {elapsed:.1f}s"
        )
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
        pnl = self.get_pnl()
        return {
            "state": {
                "runs": self.state.get("runs", 0),
                "market_phase": thermo["phase"],
                "positions_count": len(self.get_positions()),
                "last_full_run": self.state.get("last_full_run"),
            },
            "thermometer": thermo,
            "dip_status": dip,
            "portfolio": pnl,
            "capital_allocation": {
                "layer1": CAPITAL["total"] * CAPITAL["layer1_pct"],
                "layer2": CAPITAL["total"] * CAPITAL["layer2_pct"],
                "layer3": CAPITAL["total"] * CAPITAL["layer3_pct"],
                "cash_reserve": CAPITAL["total"] * CAPITAL.get("cash_reserve_pct", 0.05),
            },
        }


# ─── 单例 ───

_phoenix_instance: PhoenixRunner = None

def get_phoenix() -> PhoenixRunner:
    global _phoenix_instance
    if _phoenix_instance is None:
        _phoenix_instance = PhoenixRunner()
    return _phoenix_instance

def run_phoenix(dry_run: bool = True) -> dict:
    return get_phoenix().full_run(dry_run=dry_run)

def quick_status() -> dict:
    return get_phoenix().status()


if __name__ == "__main__":
    runner = PhoenixRunner()
    import argparse
    parser = argparse.ArgumentParser(description="Phoenix 凤凰长线策略 v3")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--run", action="store_true", help="运行（默认 dry run）")
    parser.add_argument("--live", action="store_true", help="实盘交易（需配合 --run）")
    parser.add_argument("--report", action="store_true", help="生成报告")
    parser.add_argument("--pnl", action="store_true", help="查看盈亏")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(runner.status(), indent=2, default=str))
    elif args.pnl:
        print(json.dumps(runner.get_pnl(), indent=2, default=str))
    elif args.report:
        result = runner.status()
        runner.save_report(result)
        print(json.dumps(result, indent=2, default=str))
    elif args.run:
        is_live = args.live
        if is_live:
            print("⚠️  实盘模式！5秒后开始...")
            time.sleep(5)
        results = runner.full_run(dry_run=not is_live)
        summary = results.get("summary", {})
        print(f"\nPhoenix 完成 | 第 {summary.get('run_id', 1)} 次 [{summary.get('mode', 'DRY_RUN')}]")
        print(f"市场: {summary.get('market_phase')} | 温度: {summary.get('thermo_score')}")
        pnl = summary.get("portfolio", {})
        print(f"组合: ${pnl.get('total_value', 0):,.0f} | 盈亏: {pnl.get('total_pnl_pct', 0):+.1f}%")
    else:
        # 默认 dry run
        results = runner.full_run(dry_run=True)
        summary = results.get("summary", {})
        print(f"\nPhoenix 完成 | 第 {summary.get('run_id', 1)} 次 [DRY_RUN]")
        print(f"市场: {summary.get('market_phase')} | 温度: {summary.get('thermo_score')}")
