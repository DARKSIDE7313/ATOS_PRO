"""
ATOS AutoPilot — 实时监控守护进程
===============================
持续监控系统日志，检测异常，触发 AI 诊断和自动修复。

特性:
  - 实时 tail 多日志文件
  - 智能错误检测（过滤噪音）
  - 自动触发 AI 分析
  - 安全修复自动执行
  - 30 秒内响应异常
  - 状态面板 Web API

用法:
  python3 -m atos.autopilot.monitor
"""

import os, sys, json, time, re, threading, queue
import datetime as dt
from collections import deque
from typing import Optional
from atos.core.logging import get_logger
from atos.autopilot.knowledge_base import get_stats as kb_stats, match_pattern
from atos.autopilot.ai_debugger import analyze_error, quick_check
from atos.autopilot.auto_fix import safe_fix, get_fix_history

logger = get_logger("autopilot.monitor")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# 监控的日志文件
WATCH_FILES = [
    os.path.join(BASE_DIR, "logs", "shadow_trader_stderr.log"),
    os.path.join(BASE_DIR, "logs", "atos_%s.log" % dt.date.today().strftime("%Y%m%d")),
    os.path.join(BASE_DIR, "logs", "watchdog.log"),
]

# 只对严重错误触发 AI 分析
AI_TRIGGER_PATTERNS = [
    r"(?i)critical",
    r"(?i)fatal",
    r"(?i)emergency",
    r"(?i)crash",
    r"(?i)circuit.*open",
    r"(?i)division by zero",
    r"(?i)AttributeError.*NoneType",
    r"(?i)MemoryError",
    r"(?i)disk.*(?:full|error)",
    r"(?i)DatabaseError|database.*locked",
]

# 忽略的噪音日志（不触发 AI）
IGNORE_PATTERNS = [
    r"FutuOpenD 端口.*可达",  # 正常检查
    r"实时数据源连接成功",
    r"信号缓存恢复",
    r"批量预下载完成",
    r"IC Bootstrap",
    r"Scheduler.*started",
    r"Press Ctrl\+C",
    r"监控异常检测.*完成",  # monitor 自身心跳
    r"🔍 分析错误",  # monitor 自身 AI 分析日志
    r"📋 修复建议",  # monitor 自身修复建议
    r"📚 知识库命中",  # monitor 知识库查询
    r"🤖 调用 AI 分析",  # monitor 自身 AI 调用
    r"✅ 自动修复成功",  # monitor 自身修复结果
    r"⚠️ 自动修复失败",
    r"🔧 自动修复",
    r"⚠️ Shadow Trader 进程未运行",  # 旧版误报
    r"⚠️ pgrep 找不到",  # 新版误报
    r"系统健康检查完成",
    r"autopilot\.monitor",  # monitor 自身所有日志
]


class LogMonitor:
    """实时日志监控器"""

    def __init__(self):
        self.file_positions = {}  # {filepath: last_position}
        self.error_queue = queue.Queue()
        self.fix_queue = queue.Queue()
        self.events = deque(maxlen=200)  # 最近 200 个事件
        self.start_time = time.time()
        self.errors_detected = 0
        self.auto_fixes_applied = 0
        self.ai_analyses = 0
        self.running = True
        self._init_file_positions()

    def _init_file_positions(self):
        """初始化文件读取位置（从文件末尾开始）"""
        for fp in WATCH_FILES:
            try:
                if os.path.exists(fp):
                    with open(fp) as f:
                        f.seek(0, 2)  # 跳到末尾
                        self.file_positions[fp] = f.tell()
            except Exception:
                self.file_positions[fp] = 0

        # 也检查今天日期的日志文件
        today_log = WATCH_FILES[1]
        if os.path.exists(today_log):
            try:
                with open(today_log) as f:
                    f.seek(0, 2)
                    self.file_positions[today_log] = f.tell()
            except Exception:
                pass

    def _read_new_lines(self, filepath: str) -> list:
        """读取文件新增的行"""
        lines = []
        try:
            if not os.path.exists(filepath):
                return lines

            with open(filepath) as f:
                last_pos = self.file_positions.get(filepath, 0)
                current_size = os.path.getsize(filepath)

                if current_size < last_pos:
                    # 文件被截断（log rotate），从头读
                    f.seek(0)
                else:
                    f.seek(last_pos)

                lines = f.readlines()
                self.file_positions[filepath] = f.tell()

        except Exception:
            pass

        return lines

    def _is_ai_trigger(self, line: str) -> bool:
        """判断是否需要触发 AI 分析"""
        # 忽略噪音
        for pattern in IGNORE_PATTERNS:
            if re.search(pattern, line):
                return False

        # 匹配严重错误
        for pattern in AI_TRIGGER_PATTERNS:
            if re.search(pattern, line):
                return True

        # ERROR 和 CRITICAL 级别
        if re.search(r"\|\s*(ERROR|CRITICAL)\s*\|", line):
            return True

        return False

    def _extract_error_info(self, log_lines: list) -> dict:
        """从日志行中提取错误信息"""
        combined = "\n".join(log_lines)

        info = {
            "error_type": "UNKNOWN",
            "error_message": "",
            "module": "",
            "stack_trace": "",
            "log_context": combined[-2000:],
        }

        # 解析日志格式: YYYY-MM-DD HH:MM:SS | LEVEL | module | message
        for line in log_lines:
            match = re.match(
                r'[\d-]+\s+[\d:]+\s*\|\s*(\w+)\s*\|\s*([\w.]+)\s*\|\s*(.+)',
                line
            )
            if match:
                level, module, message = match.groups()
                if level in ("ERROR", "CRITICAL"):
                    info["error_type"] = level
                    info["module"] = module
                    info["error_message"] = message[:500]

        # 提取 Traceback
        tb_match = re.search(r'(Traceback[\s\S]+?)(?=\n\d{4}-|\Z)', combined)
        if tb_match:
            info["stack_trace"] = tb_match.group(1)[:3000]

        if not info["error_message"]:
            info["error_message"] = combined[-500:]

        return info

    def _get_system_state(self) -> dict:
        """获取当前系统状态快照"""
        state = {
            "time": dt.datetime.now().isoformat(),
            "memory": {},
            "disk": {},
            "shadow": {},
            "positions": 0,
        }

        try:
            import psutil
            mem = psutil.virtual_memory()
            state["memory"] = {
                "total_gb": round(mem.total / 1e9, 1),
                "used_pct": mem.percent,
                "available_gb": round(mem.available / 1e9, 1),
            }
            disk = psutil.disk_usage(BASE_DIR)
            state["disk"] = {
                "free_gb": round(disk.free / 1e9, 1),
                "used_pct": disk.percent,
            }
        except ImportError:
            state["memory"] = {"note": "psutil not installed"}

        # 读取 Shadow 状态
        try:
            sf = os.path.join(BASE_DIR, "data", "shadow_state.json")
            if os.path.exists(sf):
                with open(sf) as f:
                    ss = json.load(f)
                state["shadow"] = {
                    "equity": ss.get("equity", 0),
                    "cash": ss.get("cash", 0),
                    "positions": len(ss.get("positions", {})),
                    "drawdown": ss.get("drawdown", 0),
                }
                state["positions"] = state["shadow"]["positions"]
        except Exception:
            pass

        return state

    def run(self):
        """主监控循环"""
        logger.info("🛡️ AutoPilot 监控系统启动")
        logger.info(f"   监控文件: {len(WATCH_FILES)} 个")
        logger.info(f"   AI触发模式: {len(AI_TRIGGER_PATTERNS)} 个")
        logger.info(f"   自动修复: 已启用")

        # 启动后台修复线程
        fix_thread = threading.Thread(target=self._fix_worker, daemon=True)
        fix_thread.start()

        while self.running:
            try:
                all_new_lines = []

                for fp in WATCH_FILES:
                    lines = self._read_new_lines(fp)
                    if lines:
                        all_new_lines.extend(lines)

                if all_new_lines:
                    # 查找需要 AI 分析的错误
                    error_lines = []
                    in_error = False

                    for line in all_new_lines:
                        line_stripped = line.strip()
                        if not line_stripped:
                            continue

                        if self._is_ai_trigger(line_stripped):
                            in_error = True
                            error_lines.append(line_stripped)
                        elif in_error:
                            # 收集后续行（可能有 traceback）
                            error_lines.append(line_stripped)
                            if len(error_lines) > 30:
                                in_error = False

                    if error_lines:
                        self.errors_detected += 1
                        error_info = self._extract_error_info(error_lines)
                        system_state = self._get_system_state()

                        self.error_queue.put({
                            "time": dt.datetime.now().isoformat(),
                            "error_info": error_info,
                            "system_state": system_state,
                            "log_lines": error_lines,
                        })

                        self._record_event("error_detected", {
                            "module": error_info["module"],
                            "message": error_info["error_message"][:100],
                        })

                # 检查系统健康
                self._health_check()

                time.sleep(5)  # 每 5 秒检查一次

            except Exception as e:
                logger.error(f"监控器内部错误: {e}")
                time.sleep(30)

    def _fix_worker(self):
        """后台修复线程：处理错误队列"""
        while self.running:
            try:
                item = self.error_queue.get(timeout=5)
            except queue.Empty:
                continue

            try:
                ei = item["error_info"]
                ss = item["system_state"]

                logger.info(f"🔍 分析错误: {ei['error_type']} in {ei['module']}")

                # AI 分析
                self.ai_analyses += 1
                result = analyze_error(
                    error_type=ei["error_type"],
                    error_message=ei["error_message"],
                    module=ei["module"],
                    stack_trace=ei.get("stack_trace", ""),
                    log_context=ei.get("log_context", ""),
                    system_state=ss,
                )

                self._record_event("ai_analysis", {
                    "known": result.get("known"),
                    "fix_type": result.get("fix_type"),
                    "can_auto_fix": result.get("can_auto_fix"),
                })

                # 如果可以自动修复，执行修复
                if result.get("can_auto_fix"):
                    logger.info(f"🔧 自动修复: {result['fix_type']}")
                    fix_result = safe_fix(
                        error_hash=result["error_hash"],
                        fix_type=result["fix_type"],
                        fix_code=result.get("fix_code", ""),
                        error_context=ei,
                    )

                    self.auto_fixes_applied += 1
                    self._record_event("auto_fix_applied", fix_result)

                    if fix_result["success"]:
                        logger.info(f"✅ 自动修复成功: {fix_result['message']}")
                    else:
                        logger.warning(f"⚠️ 自动修复失败: {fix_result['message']}")

                elif result.get("risk_level") == "dangerous":
                    logger.critical(f"🚨 危险错误需要人工介入! {result['root_cause'][:100]}")

                else:
                    logger.info(f"📋 修复建议: {result.get('fix_suggestion', '')[:100]}")

            except Exception as e:
                logger.error(f"修复线程错误: {e}")

    def _health_check(self):
        """系统健康检查 — 低噪音版"""
        # 每60秒才检查一次，避免日志刷屏
        if not hasattr(self, '_last_health_check'):
            self._last_health_check = 0
        if time.time() - self._last_health_check < 60:
            return
        self._last_health_check = time.time()

        # 检查磁盘空间
        try:
            disk = os.statvfs(BASE_DIR)
            free_gb = (disk.f_bavail * disk.f_frsize) / 1e9
            if free_gb < 0.5:
                logger.critical(f"⚠️ 磁盘空间不足: {free_gb:.1f}GB")
        except Exception:
            pass

        # 检查 Shadow Trader 进程（注意：monitor 运行在 shadow_trader 内部，
        # 所以 pgrep 必定命中自身。此检查仅用于确认 PID 文件一致性。）
        try:
            import os as _os
            my_pid = _os.getpid()
            shadow_alive = True  # monitor 自身就在这里运行

            # 确认 pgrep 能找到至少一个 shadow_trader 进程（排除自身假阳性）
            import subprocess
            # LaunchAgent 环境下 pgrep 可能不在 PATH，使用绝对路径
            pgrep_bin = "/usr/bin/pgrep"
            result = subprocess.run(
                [pgrep_bin, "-f", "shadow_trader"],
                capture_output=True, text=True, timeout=5
            )
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
            other_pids = [p for p in pids if p != my_pid]

            if not pids:
                # 完全找不到进程 → 严重问题
                shadow_alive = False
                logger.critical("⚠️ pgrep 找不到任何 shadow_trader 进程!")
            elif not other_pids and not result.stdout.strip():
                shadow_alive = False
                logger.critical("⚠️ pgrep 返回空!")
            # 如果只有自身 PID 且端口19999正常（lock port仅绑定不accept），仍算存活
        except Exception as e:
            logger.debug(f"Health check 子进程异常（非致命）: {e}")
            # monitor 自身在运行，不应误报死亡

    def _record_event(self, event_type: str, data: dict):
        """记录事件"""
        self.events.append({
            "time": dt.datetime.now().isoformat(),
            "type": event_type,
            "data": data,
        })

    def get_status(self) -> dict:
        """获取监控状态"""
        return {
            "running": self.running,
            "uptime_seconds": round(time.time() - self.start_time),
            "errors_detected": self.errors_detected,
            "auto_fixes_applied": self.auto_fixes_applied,
            "ai_analyses": self.ai_analyses,
            "knowledge_base": kb_stats(),
            "fix_history": get_fix_history(10),
            "recent_events": list(self.events)[-20:],
        }

    def stop(self):
        """停止监控"""
        self.running = False
        logger.info("AutoPilot 监控已停止")


# ============================================================
# Singleton
# ============================================================
_monitor_instance: Optional[LogMonitor] = None


def get_monitor() -> LogMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = LogMonitor()
    return _monitor_instance


# ============================================================
# CLI Entry
# ============================================================
def main():
    """启动监控守护进程"""
    import signal

    monitor = get_monitor()

    def sig_handler(signum, frame):
        logger.info(f"收到信号 {signum}，正在停止...")
        monitor.stop()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # 后台线程运行修复
    monitor.run()


if __name__ == "__main__":
    main()
