"""
ATOS PRO v3 — Phoenix 自动交易守护进程
==========================================
24/7 运行，自动：
  1. 每 60 分钟检查一次市场状态
  2. 按调度触发各层策略（Layer1每15天 / Layer2每91天 / Layer3每30天）
  3. 自动执行买入和卖出（通过Futu OpenD下单）
  4. 大跌时自动抄底
  5. 每日风险检查

用法:
  python -m atos.longterm.phoenix_daemon          # 前台运行
  python -m atos.longterm.phoenix_daemon --once   # 只运行一次（测试用）
  python -m atos.longterm.phoenix_daemon --dry    # dry run模式（不真实下单）

LaunchAgent 自动启动: com.atos.phoenix.plist
"""

import os, sys, json, time, datetime, signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atos.core.logging import get_logger
from atos.longterm.phoenix_runner import PhoenixRunner
from atos.longterm.config import CAPITAL, SCHEDULE
from atos.longterm.futu_watchdog import FutuWatchdog  # v5: 启用 — 统一管理FutuOpenD恢复

logger = get_logger("phoenix.daemon")

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PID_FILE = os.path.join(BASE, "data", ".phoenix_daemon.pid")


class PhoenixDaemon:
    """
    Phoenix 自动交易守护进程。

    设计思路：
      - 不是高频交易，是长线策略
      - 每 60 分钟醒来一次，检查"该不该做点什么"
      - 不频繁调仓——Layer1每15天、Layer2每季度、Layer3每月
      - 但每天都会检查 market regime 和风险
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.runner = PhoenixRunner()
        self.running = True
        self.check_interval = 1800  # v11: 30 分钟检查一次 (原60分钟)
        self.last_dip_check = None
        self.start_time = datetime.datetime.now()
        self.watchdog = FutuWatchdog()  # Futu 看门狗

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"收到信号 {signum}，优雅退出...")
        self.running = False

    def write_pid(self):
        try:
            os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
            with open(PID_FILE, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

    def remove_pid(self):
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except Exception:
            pass

    def is_market_hours(self) -> bool:
        """判断是否在美股交易时段（含盘前30分钟）"""
        try:
            from atos.live.futu_bridge import is_market_open
            return is_market_open()[0]
        except Exception:
            # 简易判断
            now = datetime.datetime.now(datetime.timezone.utc)
            if now.weekday() >= 5:
                return False
            hour = now.hour + now.minute / 60
            return 13.0 <= hour <= 20.5  # 9am-4:30pm ET

    def run_once(self) -> dict:
        """执行一次完整检查"""
        mode = "DRY_RUN" if self.dry_run else "LIVE"
        logger.info(f"🔥 Phoenix 自动检查开始 [{mode}] | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")

        result = {"timestamp": datetime.datetime.now().isoformat(), "mode": mode, "actions": []}

        try:
            # 1. 市场温度（每小时快速检查，每天首次完整检查）
            hour_of_day = datetime.datetime.now().hour
            use_fast = not (6 <= hour_of_day <= 7)  # 早上6-7点做完整检查
            thermo = self.runner.thermometer.comprehensive_score(fast=use_fast)
            self.runner.state["market_phase"] = thermo["phase"]
            result["market"] = {"phase": thermo["phase"], "score": thermo["score"]}

            # 2. 每日风险检查（用真实组合市值，不是0）
            if self.runner.should_run_layer("risk"):
                portfolio_value = self.runner.get_portfolio_value()
                if portfolio_value <= 0:
                    portfolio_value = self.runner.state.get("cash", CAPITAL["total"])
                positions = self.runner.get_positions()
                risk = self.runner.risk.full_check(positions=positions, current_value=portfolio_value)
                result["risk"] = {"alerts": risk.get("alerts_count", 0), "pass": risk.get("pass", True)}
                if risk.get("actions_required"):
                    for action in risk["actions_required"]:
                        logger.warning(f"⚠️ 风险操作: {action}")
                        result["actions"].append({"type": "risk_action", "detail": action})
                self.runner.state["risk_last_check"] = datetime.datetime.now().isoformat()

            # 3. 抄底检查（每小时）
            now = datetime.datetime.now()
            if self.last_dip_check is None or (now - self.last_dip_check).seconds > 3600:
                from atos.longterm.cash_manager import should_buy_the_dip
                dip = should_buy_the_dip()
                result["dip"] = dip
                if dip.get("deploy"):
                    logger.info(f"💵 抄底触发: {dip['reason']} — 部署 {dip['pct']*100:.0f}% 现金")
                    result["actions"].append({"type": "dip_buy", "detail": dip})
                self.last_dip_check = now

            # 4. 各层策略（按调度）
            positions = self.runner.get_positions()
            cash_pct = self.runner.state.get("cash", 0) / max(self.runner.state.get("total_deposited", 1), 1)

            # v11: 如果现金>50%闲置且持仓<5，强制触发所有层（加速部署）
            force_all = (cash_pct > 0.50 and len(positions) < 5)
            if force_all:
                logger.info(f"🚀 现金{cash_pct:.0%}闲置+仅{len(positions)}持仓 → 强制触发所有层!")

            # 4a. 卖出检查（每次都跑，不限于调度周期）
            all_sell_orders = []
            try:
                all_sell_orders.extend(self.runner.layer1.get_sell_orders(positions))
            except Exception as e:
                logger.warning(f"L1卖出检查异常: {e}")
            try:
                all_sell_orders.extend(self.runner.layer2.get_sell_orders(positions))
            except Exception as e:
                logger.warning(f"L2卖出检查异常: {e}")
            try:
                all_sell_orders.extend(self.runner.layer3.get_sell_orders(positions))
            except Exception as e:
                logger.warning(f"L3卖出检查异常: {e}")

            # 4b. 各层买入（按调度周期，或强制触发）
            all_buy_orders = []

            if force_all or self.runner.should_run_layer("layer1"):
                try:
                    self.runner.layer1.run()
                    all_buy_orders.extend(self.runner.layer1.get_buy_orders(positions))
                    self.runner.state["layer1_last_run"] = datetime.datetime.now().isoformat()
                    result["layer1"] = "executed"
                except Exception as e:
                    logger.error(f"L1执行失败: {e}")
                    result["layer1"] = f"error: {e}"

            if force_all or self.runner.should_run_layer("layer2"):
                try:
                    self.runner.layer2.run()
                    all_buy_orders.extend(self.runner.layer2.get_buy_orders(positions))
                    self.runner.state["layer2_last_run"] = datetime.datetime.now().isoformat()
                    result["layer2"] = "executed"
                except Exception as e:
                    logger.error(f"L2执行失败: {e}")
                    result["layer2"] = f"error: {e}"

            if force_all or self.runner.should_run_layer("layer3"):
                try:
                    self.runner.layer3.run()
                    all_buy_orders.extend(self.runner.layer3.get_buy_orders(positions))
                    self.runner.state["layer3_last_run"] = datetime.datetime.now().isoformat()
                    result["layer3"] = "executed"
                except Exception as e:
                    logger.error(f"L3执行失败: {e}")
                    result["layer3"] = f"error: {e}"

            # 5. 合并并执行订单（卖单优先）
            all_orders = all_sell_orders + all_buy_orders
            result["orders"] = {"sell": len(all_sell_orders), "buy": len(all_buy_orders)}

            if all_orders:
                # 去重
                all_orders = self.runner._deduplicate_orders(all_orders)

                if all_orders:
                    # Tactical Overlay 过滤
                    from atos.longterm.tactical_overlay import get_overlay
                    overlay = get_overlay()
                    all_orders = overlay.adjust_for_regime(all_orders)
                    all_orders, _ = overlay.screen_orders(all_orders)

                if all_orders:
                    # 只在市场开盘时执行真实订单
                    if self.is_market_hours() or self.dry_run:
                        exec_result = self.runner.execute_orders(all_orders, dry_run=self.dry_run)
                        result["execution"] = exec_result
                        logger.info(
                            f"📊 订单执行: {exec_result['executed']}/{len(all_orders)} 成功 "
                            f"({'DRY_RUN' if self.dry_run else 'LIVE'})"
                        )
                    else:
                        result["execution"] = {"status": "waiting_market_open", "pending": len(all_orders)}
                        logger.info(f"⏰ 非交易时段，{len(all_orders)} 个订单等待开盘执行")

            # 6. 保存状态
            self.runner.state["runs"] = self.runner.state.get("runs", 0) + 1
            self.runner.state["last_full_run"] = datetime.datetime.now().isoformat()
            self.runner.save_state()

            # 7. 打印组合摘要
            pnl = self.runner.get_pnl()
            logger.info(
                f"📈 Phoenix 状态 | 组合 ${pnl['total_value']:,.0f} "
                f"({pnl['total_pnl_pct']:+.1f}%) | "
                f"{len(positions)}只持仓 | "
                f"{len(all_sell_orders)}卖 {len(all_buy_orders)}买 | "
                f"市场 {thermo['phase']}"
            )

        except Exception as e:
            logger.error(f"Phoenix 运行异常: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    def run_forever(self):
        """持续运行，每次检查间隔 sleep"""
        self.write_pid()
        mode = "DRY_RUN" if self.dry_run else "LIVE"
        logger.info(f"🚀 Phoenix 守护进程启动 [{mode}] | 检查间隔: {self.check_interval}s")
        logger.info(f"   资金: ${CAPITAL['total']:,.0f} | L1:{CAPITAL['layer1_pct']:.0%} L2:{CAPITAL['layer2_pct']:.0%} L3:{CAPITAL['layer3_pct']:.0%}")
        logger.info(f"   PID: {os.getpid()}")

        # 启动 Futu 看门狗
        self.watchdog.start()
        logger.info(f"   👀 Futu 看门狗已启动 (每120s检查 Futu 连接)")

        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"主循环异常: {e}", exc_info=True)

            if not self.running:
                break

            # Sleep with heartbeats
            logger.debug(f"💤 休眠 {self.check_interval}s...")
            for _ in range(self.check_interval // 10):
                if not self.running:
                    break
                time.sleep(10)

        self.watchdog.stop()
        self.remove_pid()
        uptime = datetime.datetime.now() - self.start_time
        logger.info(f"👋 Phoenix 守护进程退出 | 运行时长: {uptime}")


# ═══════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Phoenix 自动交易守护进程")
    parser.add_argument("--once", action="store_true", help="只运行一次（测试用）")
    parser.add_argument("--dry", action="store_true", help="dry run 模式（不真实下单）")
    parser.add_argument("--interval", type=int, default=1800, help="检查间隔（秒，默认1800）")
    args = parser.parse_args()

    daemon = PhoenixDaemon(dry_run=args.dry)
    daemon.check_interval = args.interval

    if args.once:
        result = daemon.run_once()
        print(json.dumps(result, indent=2, default=str))
    else:
        daemon.run_forever()
