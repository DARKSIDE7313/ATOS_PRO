"""
ATOS PRO — 市场时钟单一真源 (Phase 5 框架重塑)
=================================================
历史问题: 三套市场时钟并存 —
  1. atos/live/futu_bridge.py::is_market_open   (唯一正确: zoneinfo DST + 假日)
  2. atos/debugger/safety_net.py::is_safe_to_trade (手算 DST 近似, 无假日)
  3. atos/live/signal_engine.py::is_nasdaq_open   (UTC offset 推断, 无假日)

本模块是唯一权威实现 (逻辑逐字取自 futu_bridge，全部调用方改为委派到这里)。
时区判断一律走 zoneinfo (线程安全, 不修改全局 TZ)。
"""

import datetime

# 美股假日（2026年）
US_HOLIDAYS_2026 = {
    datetime.date(2026, 1, 1):   "元旦",
    datetime.date(2026, 1, 19):  "马丁路德金日",
    datetime.date(2026, 2, 16):  "总统日",
    datetime.date(2026, 4, 3):   "耶稣受难日",
    datetime.date(2026, 5, 25):  "阵亡将士纪念日",
    datetime.date(2026, 6, 19):  "六月节",
    datetime.date(2026, 7, 3):   "独立日(观察)",
    datetime.date(2026, 9, 7):   "劳动节",
    datetime.date(2026, 11, 26): "感恩节",
    datetime.date(2026, 12, 25): "圣诞节",
    # 半天交易日（1pm收盘）
    datetime.date(2026, 11, 27): "黑色星期五(半天)",
    datetime.date(2026, 12, 24): "圣诞前夕(半天)",
}


def is_edt_now() -> bool:
    """判断当前是否为美国东部夏令时 (EDT) — 线程安全（zoneinfo，不修改全局 TZ）"""
    from zoneinfo import ZoneInfo
    try:
        now_ny = datetime.datetime.now(ZoneInfo("America/New_York"))
        # EDT 期间 dst() 返回 1 小时，EST 返回 0；bool(timedelta) 直接给出判断
        return bool(now_ny.dst())
    except Exception:
        return False


def is_market_open() -> tuple:
    """
    检查美股是否在交易（自动识别夏令时/冬令时 + 2026 假日表）。
    返回 (是否开市, 原因说明)
    """
    from zoneinfo import ZoneInfo
    now = datetime.datetime.now(datetime.timezone.utc)
    ny_now = now.astimezone(ZoneInfo("America/New_York"))
    today = ny_now.date()  # M3: 用美东日期判断假日/周末（而非 UTC 日期）

    # 周末（美东时间）
    if ny_now.weekday() >= 5:
        return False, "周末休市"

    # 假日（美东日期）
    if today in US_HOLIDAYS_2026:
        return False, f"假日休市: {US_HOLIDAYS_2026[today]}"

    # EDT (夏令时 3月-11月): 开盘 13:30 UTC, 收盘 20:00 UTC
    # EST (冬令时): 开盘 14:30 UTC, 收盘 21:00 UTC
    is_edt = is_edt_now()
    open_hour, close_hour = (13, 20) if is_edt else (14, 21)

    # 半天交易日
    half_day = US_HOLIDAYS_2026.get(today, "")
    if "半天" in half_day:
        open_t = now.replace(hour=open_hour, minute=30, second=0)
        close_t = now.replace(hour=open_hour + 4, minute=0, second=0)  # 1pm local
    else:
        open_t = now.replace(hour=open_hour, minute=30, second=0)   # 9:30am local
        close_t = now.replace(hour=close_hour, minute=0, second=0)   # 4:00pm local

    if now < open_t:
        return False, f"盘前 (距开盘 {(open_t - now).seconds // 60} 分钟)"
    if now > close_t:
        return False, "已收盘"

    return True, "正常交易"


def get_market_date() -> datetime.date:
    """返回当前美东市场日期 (跨天检测单一真源)。

    供日级风控重置等跨天逻辑使用 — 与 is_market_open() 用同一时区/假日基准。
    """
    from zoneinfo import ZoneInfo
    try:
        return datetime.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return datetime.datetime.now().date()


def is_open_now() -> bool:
    """布尔便捷接口 (供只关心 True/False 的调用方)"""
    return is_market_open()[0]
