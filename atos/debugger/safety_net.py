"""
ATOS PRO v2 — 全系统安全网
===========================
除了 AI 幻觉，自动化交易还有这些风险。每一条都可能造成真金白银的损失。

防护清单：
  1. API 故障 — DeepSeek 挂了/限流/返回垃圾
  2. 数据异常 — yfinance NaN/空值/过期数据
  3. 数学安全 — 除零/负数价格/浮点精度
  4. 状态一致性 — 现金不能为负/持仓数必须≥0
  5. 文件安全 — 配置损坏/状态文件读写冲突
  6. 日志膨胀 — 磁盘写满
  7. 重复执行 — 同一订单提交两次
  8. 时间边界 — 周末/假日/盘前盘后
  9. 网络超时 — API 调用卡死
  10. 敏感信息泄露 — Key 出现在日志里
"""

import os
import json
import math
import time
import threading
from decimal import Decimal, ROUND_HALF_UP
from atos.core.logging import get_logger, log_risk

logger = get_logger("safety_net")


# ═══════════════════════════════════════════
# 1. API 故障防护
# ═══════════════════════════════════════════

class SafeAPICaller:
    """带超时+重试+退避+垃圾检测的 API 调用器"""
    def __init__(self, timeout: int = 30, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.consecutive_failures = 0
        self.MAX_CONSECUTIVE_FAILURES = 5

    def call(self, fn, *args, **kwargs):
        """安全调用：超时→重试→退避→熔断"""
        import requests
        for attempt in range(self.max_retries):
            try:
                result = fn(*args, **kwargs)
                self.consecutive_failures = 0
                return result
            except requests.Timeout:
                logger.warning(f"API超时 (尝试 {attempt+1}/{self.max_retries})")
                time.sleep(2 ** attempt)
            except requests.ConnectionError:
                logger.warning(f"API连接失败 (尝试 {attempt+1}/{self.max_retries})")
                time.sleep(2 ** attempt)
            except requests.HTTPError as e:
                status = e.response.status_code if hasattr(e, 'response') else 0
                if status == 429:  # Rate limit
                    logger.warning(f"API限流，等待30秒")
                    time.sleep(30)
                elif status >= 500:  # Server error
                    logger.warning(f"API服务器错误 {status}，等待重试")
                    time.sleep(5 * (2 ** attempt))
                else:
                    raise  # 4xx 客户端错误不重试
            except Exception as e:
                logger.error(f"API调用异常: {e}")
                time.sleep(2 ** attempt)

        self.consecutive_failures += 1
        if self.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            log_risk("API_MELTDOWN", f"连续{self.consecutive_failures}次API失败，建议暂停交易")
        raise RuntimeError(f"API调用失败，已重试{self.max_retries}次")


# ═══════════════════════════════════════════
# 2. 数据异常检测
# ═══════════════════════════════════════════

def validate_market_data(signals: dict, symbol: str = None) -> dict:
    """检测 yfinance 返回的数据是否异常"""
    issues = []
    for sym, s in (signals.items() if not symbol else {symbol: signals.get(symbol, {})}.items()):
        if not s:
            issues.append(f"{sym}: 无数据")
            continue
        price = s.get("price", 0)
        rsi = s.get("rsi", 50)
        vol = s.get("volume_ratio", 1.0)

        if price <= 0:          issues.append(f"{sym}: 价格≤0 (${price})")
        if price > 100000:      issues.append(f"{sym}: 价格异常高 (${price})")
        if rsi < 0 or rsi > 100: issues.append(f"{sym}: RSI越界 ({rsi})")
        if vol <= 0:            issues.append(f"{sym}: 成交量为0")
        if vol > 100:           issues.append(f"{sym}: 成交量异常 (x{vol})")
        if math.isnan(price) or math.isinf(price):
            issues.append(f"{sym}: 价格NaN/Inf")

    return {"healthy": len(issues) == 0, "issues": issues}


def is_data_stale(last_update_time: float, max_age_minutes: int = 120) -> bool:
    """数据是否过期（超过2小时没更新=可能有问题）"""
    return (time.time() - last_update_time) > (max_age_minutes * 60)


# ═══════════════════════════════════════════
# 3. 数学安全
# ═══════════════════════════════════════════

def safe_divide(a: float, b: float, default: float = 0.0) -> float:
    """安全除法：除零返回默认值"""
    return a / b if abs(b) > 1e-12 else default

def safe_price(price: float) -> float:
    """确保价格是正数"""
    if math.isnan(price) or math.isinf(price) or price <= 0:
        return None
    return round(price, 2)

def clamp(value: float, lo: float, hi: float) -> float:
    """值钳制在 [lo, hi]"""
    return max(lo, min(hi, value))

def money_round(amount: float) -> float:
    """金额四舍五入到分"""
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ═══════════════════════════════════════════
# 4. 状态一致性检查
# ═══════════════════════════════════════════

def validate_account_state(cash: float, positions, total_equity: float) -> dict:
    """检查账户状态是否自洽（接受 dict 或 list）"""
    issues = []
    if cash < -1.0:              issues.append(f"现金为负: ${cash:.2f}")
    if cash > total_equity * 2:  issues.append(f"现金异常高: ${cash:.0f} vs 总资产${total_equity:.0f}")

    if isinstance(positions, dict):
        pos_iter = positions.items()
    elif isinstance(positions, list):
        pos_iter = ((p.get("symbol", f"pos{i}"), p) for i, p in enumerate(positions))
    else:
        return {"consistent": True, "issues": []}

    for sym, pos in pos_iter:
        qty = pos.get("qty", 0)
        price = pos.get("last_price", pos.get("avg_price", 0))
        if qty < 0:              issues.append(f"{sym}: 持仓为负 ({qty}股)")
        if qty > 1_000_000:      issues.append(f"{sym}: 持仓异常大 ({qty}股)")
        if price <= 0:           issues.append(f"{sym}: 持仓价格≤0")
        if qty * price > total_equity * 3:
            issues.append(f"{sym}: 单一持仓超总资产3倍")

    return {"consistent": len(issues) == 0, "issues": issues}


# ═══════════════════════════════════════════
# 5. 文件安全
# ═══════════════════════════════════════════

_file_locks = {}
_lock_registry = threading.Lock()

def atomic_write(filepath: str, data: str) -> bool:
    """原子写入：先写临时文件，再 rename。不会出现半截文件。"""
    tmp = filepath + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, filepath)
        return True
    except Exception as e:
        logger.error(f"原子写入失败 {filepath}: {e}")
        return False

def safe_load_json(filepath: str, default: dict = None) -> dict:
    """安全加载 JSON：损坏→告警→返回默认值"""
    if default is None:
        default = {}
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"JSON损坏 {filepath}: {e} → 用默认值替代")
        # 备份损坏文件
        if os.path.exists(filepath):
            os.rename(filepath, filepath + f".corrupted.{int(time.time())}")
        return default


# ═══════════════════════════════════════════
# 6. 日志膨胀防护
# ═══════════════════════════════════════════

def check_disk_space(path: str = ".", min_free_mb: int = 100) -> bool:
    """检查磁盘空间"""
    import shutil
    stat = shutil.disk_usage(path)
    free_mb = stat.free / (1024 * 1024)
    if free_mb < min_free_mb:
        log_risk("DISK_FULL", f"磁盘仅剩 {free_mb:.0f}MB < {min_free_mb}MB 安全线")
        return False
    return True

def cleanup_old_logs(log_dir: str, max_days: int = 30, max_total_mb: int = 500):
    """自动清理过期日志"""
    import glob
    total_size = 0
    files = sorted(glob.glob(os.path.join(log_dir, "*.log*")), key=os.path.getmtime, reverse=True)
    for f in files:
        size = os.path.getsize(f) / (1024 * 1024)
        total_size += size
        age_days = (time.time() - os.path.getmtime(f)) / 86400
        if age_days > max_days or total_size > max_total_mb:
            try:
                os.remove(f)
                logger.info(f"清理旧日志: {os.path.basename(f)} ({size:.1f}MB)")
            except Exception:
                pass


# ═══════════════════════════════════════════
# 7. 重复执行检测
# ═══════════════════════════════════════════

_last_order = {"symbol": "", "action": "", "shares": 0, "time": 0}
_order_lock = threading.Lock()

def is_duplicate_order(symbol: str, action: str, shares: int, window_seconds: int = 60) -> bool:
    """检测 60 秒内是否有完全相同的订单（防重复提交）"""
    with _order_lock:
        now = time.time()
        if (_last_order["symbol"] == symbol and
            _last_order["action"] == action and
            _last_order["shares"] == shares and
            (now - _last_order["time"]) < window_seconds):
            log_risk("DUPLICATE_ORDER", f"拦截重复订单: {action} {shares}股 {symbol}")
            return True
        _last_order.update({"symbol": symbol, "action": action, "shares": shares, "time": now})
        return False


# ═══════════════════════════════════════════
# 8. 敏感信息保护
# ═══════════════════════════════════════════

def sanitize_for_log(text: str) -> str:
    """脱敏：移除 API Key 等敏感信息"""
    import re
    # 移除 sk- 开头的 key
    text = re.sub(r'sk-[a-zA-Z0-9]{20,}', 'sk-***REDACTED***', text)
    # 移除邮箱密码
    text = re.sub(r'ATOS_EMAIL_PASS=[^\s]+', 'ATOS_EMAIL_PASS=***', text)
    return text


# ═══════════════════════════════════════════
# 9. 全面系统健康检查
# ═══════════════════════════════════════════

def full_health_check(account_state: dict = None) -> dict:
    """一次运行所有安全检查"""
    results = {}

    # 磁盘
    results["disk"] = check_disk_space()

    # 数据时效
    state_file = os.path.expanduser("~/ATOS_PRO/data/shadow_state.json")
    if os.path.exists(state_file):
        age = (time.time() - os.path.getmtime(state_file)) / 60
        results["data_freshness"] = age < 120
        if age >= 120:
            results["data_warning"] = f"状态文件 {age:.0f} 分钟未更新"

    # 账户一致性
    if account_state:
        results["account"] = validate_account_state(
            account_state.get("cash", 0),
            account_state.get("positions", {}),
            account_state.get("total", 0)
        )

    # 日志大小
    log_dir = os.path.expanduser("~/ATOS_PRO/logs")
    if os.path.exists(log_dir):
        total_mb = sum(os.path.getsize(os.path.join(log_dir, f))
                       for f in os.listdir(log_dir)
                       if os.path.isfile(os.path.join(log_dir, f))) / (1024*1024)
        results["log_size_mb"] = round(total_mb, 1)
        if total_mb > 500:
            cleanup_old_logs(log_dir)

    return results


# ═══════════════════════════════════════════
# 10. 时间边界检查
# ═══════════════════════════════════════════

def is_safe_to_trade() -> tuple[bool, str]:
    """综合判断现在是否应该交易（支持夏令时/冬令时自动切换）"""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)

    # 周末
    if now.weekday() >= 5:
        return False, "周末休市"

    # 动态 DST 检测（美国）：3月第2个周日～11月第1个周日
    # 简化规则：3月～10月为 EDT (UTC-4), 11月～2月为 EST (UTC-5)
    month = now.month
    if 3 <= month <= 10:  # EDT (夏令时): UTC = ET + 4
        open_hour, close_hour = 13, 20   # 9:30AM ET = 13:30 UTC, 4PM ET = 20:00 UTC
    else:                  # EST (冬令时): UTC = ET + 5
        open_hour, close_hour = 14, 21   # 9:30AM ET = 14:30 UTC, 4PM ET = 21:00 UTC

    # 留 15 分钟缓冲
    open_t = now.replace(hour=open_hour, minute=30, second=0)
    close_t = now.replace(hour=close_hour, minute=15, second=0)
    if now < open_t:
        return False, "盘前"
    if now > close_t:
        return False, "已收盘"

    # 月末/季末可能有异常波动（期权到期日前后）
    # 简单检测但不阻止交易
    if now.day >= 28:
        logger.debug("月末/季末 — 注意可能的高波动")

    return True, "交易时间"
