"""
ATOS PRO v3 — Futu OpenD 看门狗
==================================
独立线程，每 2 分钟检查 Futu 连接状态。
掉线 → macOS 通知 + 日志报警 + 自动重连。
恢复 → 通知用户。

用法:
  python -m atos.longterm.futu_watchdog          # 独立运行
  或在 PhoenixDaemon 中自动启动
"""

import os, time, socket, subprocess, datetime, threading
from atos.core.logging import get_logger

logger = get_logger("phoenix.futu_watchdog")

FUTU_HOST = "127.0.0.1"
FUTU_PORT = 11111
CHECK_INTERVAL = 120  # 2分钟检查一次
MAX_RETRY_ATTEMPTS = 3
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.futunn.FutuOpenD.plist")

# v10: 智能退避 — 连续失败越多，检查间隔越长
BACKOFF_SCHEDULE = {
    5:   300,    # 5次失败后 → 5分钟间隔
    10:  900,    # 10次失败后 → 15分钟间隔
    20:  1800,   # 20次失败后 → 30分钟间隔
    50:  7200,   # 50次失败后 → 2小时间隔 (发送通知)
}


class FutuWatchdog:
    """
    Futu OpenD 看门狗。

    功能：
      1. 每2分钟检查 Futu 连接
      2. 掉线 → macOS 桌面通知 + 日志
      3. 自动重启 FutuOpenD
      4. 恢复 → 通知
    """

    def __init__(self):
        self._last_status = True  # 假设初始正常
        self._consecutive_failures = 0
        self._running = False
        self._thread = None
        self._alert_cooldown = 0  # 防止通知轰炸
        self._ctx = None  # v5: 复用 OpenQuoteContext，不每次新建

    # ═══════════════════════════════════════════
    # 连接检查
    # ═══════════════════════════════════════════

    def check_futu(self) -> bool:
        """检查 Futu OpenD 是否正常 (v5: 复用连接)"""
        # TCP 端口检查
        try:
            sock = socket.create_connection((FUTU_HOST, FUTU_PORT), timeout=3)
            sock.close()
        except Exception:
            return False

        # API 功能检查 — 复用已有连接
        try:
            from futu import OpenQuoteContext, RET_OK
            if self._ctx is None:
                self._ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            ret, data = self._ctx.get_market_snapshot(["US.AAPL"])
            return ret == RET_OK
        except Exception:
            # 连接坏了，关闭重建
            if self._ctx:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
            return False

    def is_futu_process_alive(self) -> bool:
        """Futu_OpenD 进程是否活着"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "Futu_OpenD"],
                capture_output=True, text=True, timeout=5
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    # ═══════════════════════════════════════════
    # 报警
    # ═══════════════════════════════════════════

    def send_notification(self, title: str, message: str, sound: bool = True):
        """发送 macOS 桌面通知"""
        try:
            sound_arg = 'sound name "Glass"' if sound else ''
            script = f'display notification "{message}" with title "{title}" {sound_arg}'
            subprocess.run(["osascript", "-e", script], timeout=5)
        except Exception as e:
            logger.warning(f"通知发送失败: {e}")

    def alert_down(self):
        """Futu 掉线报警"""
        # 冷却：30分钟内最多报警1次（降噪）
        now = time.time()
        if now - self._alert_cooldown < 1800:  # 30分钟冷却
            return
        self._alert_cooldown = now

        msg = f"Futu OpenD 掉线！端口 {FUTU_PORT} 无响应。"
        logger.warning(f"⚠️ {msg} (下次检查: {CHECK_INTERVAL}s后)")
        self.send_notification("⚠️ ATOS: Futu 掉线", msg)

    def alert_recovered(self, downtime_seconds: float = 0):
        """Futu 恢复通知"""
        mins = int(downtime_seconds / 60)
        msg = f"Futu OpenD 已恢复连接（中断约 {mins} 分钟）"
        logger.info(f"✅ {msg}")
        self.send_notification("✅ ATOS: Futu 已恢复", msg, sound=False)

    # ═══════════════════════════════════════════
    # 自动重连
    # ═══════════════════════════════════════════

    def restart_futu(self) -> bool:
        """尝试重启 FutuOpenD（用 open -a 启动 GUI）"""
        try:
            # 策略: 用 open -a 启动（GUI 方式）
            subprocess.run(
                ["open", "-a", "Futu_OpenD"],
                capture_output=True, timeout=10
            )
            logger.info("🔄 FutuOpenD GUI 启动指令已发送")

            # 等它启动
            for i in range(15):
                time.sleep(2)
                if self.check_futu():
                    logger.info("✅ FutuOpenD 重启成功")
                    return True

            logger.warning("⚠️ 30秒后 FutuOpenD 仍未就绪 (可能需要手动登录)")
            return False

        except Exception as e:
            logger.warning(f"重启异常: {e}")
            return False

    # ═══════════════════════════════════════════
    # 主循环
    # ═══════════════════════════════════════════

    def _get_sleep_seconds(self) -> int:
        """v10: 根据连续失败次数计算退避间隔"""
        interval = CHECK_INTERVAL
        for threshold, backoff in sorted(BACKOFF_SCHEDULE.items()):
            if self._consecutive_failures >= threshold:
                interval = backoff
        return interval

    def _is_weekend(self) -> bool:
        """v10: 判断是否周末（美国时间）"""
        import datetime as _dt
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        # 美国东部时间 = UTC-4(EDT) 或 UTC-5(EST)
        est_hour = (now_utc.hour - 4 + 24) % 24
        est_day = now_utc.weekday()
        # 周五16:00后到周日全天 = 周末
        if est_day == 4 and est_hour >= 16:
            return True
        if est_day == 5:  # 周六
            return True
        if est_day == 6 and est_hour < 18:  # 周日下午6点前（美东）
            return True
        return False

    def _loop(self):
        """后台线程主循环 (v10: 智能退避)"""
        logger.info(f"👀 Futu 看门狗启动 | 每 {CHECK_INTERVAL}s 检查一次 {FUTU_HOST}:{FUTU_PORT}")

        while self._running:
            try:
                # v10: 周末跳过 (节省资源)
                if self._is_weekend():
                    logger.debug("📴 周末休市 — 跳过 Futu 检查")
                    for _ in range(3600):  # 睡1小时再检查
                        if not self._running:
                            break
                        time.sleep(1)
                    continue

                ok = self.check_futu()

                if ok:
                    if not self._last_status:
                        # 刚恢复
                        self.alert_recovered()
                    self._last_status = True
                    self._consecutive_failures = 0

                else:
                    self._consecutive_failures += 1
                    # v10: 智能退避日志
                    sleep_sec = self._get_sleep_seconds()
                    if self._consecutive_failures <= 3:
                        logger.debug(f"Futu 无响应 (连续 {self._consecutive_failures} 次)")
                    elif self._consecutive_failures % 10 == 0:
                        logger.warning(
                            f"⚠️ Futu 无响应 (连续 {self._consecutive_failures} 次, "
                            f"下次检查 {sleep_sec}s 后)"
                        )

                    # v10: 50次失败后发送通知提醒用户手动登录
                    if self._consecutive_failures == 50:
                        self.send_notification(
                            "🔴 ATOS: FutuOpenD 需要登录",
                            "FutuOpenD 已连续失败50次。请手动打开 FutuOpenD 并登录。"
                        )

                    if self._last_status:
                        # 刚从正常转为掉线
                        self.alert_down()

                    self._last_status = False

                    # 连续3次失败 → 尝试重启 (每30次最多重启一次)
                    if self._consecutive_failures >= MAX_RETRY_ATTEMPTS and self._consecutive_failures % 30 == 3:
                        logger.info("🔄 尝试自动重启 FutuOpenD...")
                        if self.restart_futu():
                            self._consecutive_failures = 0
                        else:
                            logger.debug("自动重启未成功，继续等待")

            except Exception as e:
                logger.error(f"看门狗异常: {e}")

            # v10: 智能退避间隔
            sleep_sec = self._get_sleep_seconds() if not ok else CHECK_INTERVAL
            for _ in range(sleep_sec):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("👋 Futu 看门狗退出")

    def start(self):
        """启动看门狗（后台线程）"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="futu-watchdog")
        self._thread.start()

    def stop(self):
        """停止看门狗"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        # v5: 清理连接
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None


# ═══════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import signal
    wd = FutuWatchdog()

    def shutdown(sig, frame):
        print("\n停止看门狗...")
        wd.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    wd.start()
    print(f"👀 Futu 看门狗运行中 (PID {os.getpid()}) — 按 Ctrl+C 停止")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(None, None)
