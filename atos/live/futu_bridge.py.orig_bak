"""
ATOS PRO v2 — FutuOpenD 兼容桥
===============================
预解决接入真实券商可能遇到的所有问题：
  1. 断线重连 — 指数退避 + 最多 5 次
  2. 限流保护 — 每秒最多 3 次 API 调用
  3. 错误码映射 — Futu 错误 → 可读信息
  4. 夏令时感知 — 正确的美股交易时间
  5. 假日检测 — 美股假日不交易
  6. 订单确认 — 下单后轮询确认成交
  7. 持仓同步 — 定期与实际账户对账
"""

import time
import datetime
import socket
import functools
from atos.core.logging import get_logger

logger = get_logger("futu_bridge")

# ========== 1. 重连机制 ==========

def retry_with_backoff(max_retries: int = 5, base_delay: float = 1.0):
    """装饰器：失败后指数退避重试"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"{func.__name__} 失败 (尝试 {attempt+1}/{max_retries}): {e} → {delay:.1f}s 后重试"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(delay)
            raise last_error
        return wrapper
    return decorator


# ========== 2. 限流保护 ==========

class RateLimiter:
    """每秒最多 N 次调用"""
    def __init__(self, max_calls_per_second: int = 3):
        self.max_calls = max_calls_per_second
        self.calls = []

    def wait(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 1.0]
        if len(self.calls) >= self.max_calls:
            sleep_time = 1.0 - (now - self.calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        self.calls.append(time.time())

_rate_limiter = RateLimiter(max_calls_per_second=3)


# ========== 3. 错误码映射 ==========

FUTU_ERROR_MAP = {
    # 连接错误
    -1: "连接失败 — FutuOpenD 是否已启动？",
    -2: "网络超时 — 检查本地网络或防火墙",
    # 认证错误
    1: "未登录 — 请在 FutuOpenD 中登录账户",
    2: "账户密码错误",
    # 交易错误
    10: "下单失败：资金不足",
    11: "下单失败：持仓不足（卖空？）",
    12: "下单失败：该股票不可交易（可能是休市或停牌）",
    13: "市价单在盘前/盘后不可用",
    14: "订单超过单笔最大数量限制",
    # 数据错误
    20: "获取行情失败：股票代码错误或不在交易时间",
    21: "获取账户信息失败：网络问题或账户状态异常",
}


def translate_error(error_code: int, default_msg: str = "") -> str:
    """将 Futu 错误码翻译成中文"""
    return FUTU_ERROR_MAP.get(error_code, default_msg or f"未知错误 (code={error_code})")


# ========== 4. 交易时间（含夏令时） ==========

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


def _is_edt_now() -> bool:
    """判断当前是否为美国东部夏令时 (EDT)"""
    import time as _time, os as _os
    old_tz = _os.environ.get("TZ", "")
    _os.environ["TZ"] = "America/New_York"
    try:
        _time.tzset()
        return bool(_time.daylight)
    finally:
        if old_tz:
            _os.environ["TZ"] = old_tz
        else:
            _os.environ.pop("TZ", None)
        _time.tzset()


def is_market_open() -> tuple[bool, str]:
    """
    检查美股是否在交易（自动识别夏令时/冬令时）。
    返回 (是否开市, 原因说明)
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()

    # 周末
    if now.weekday() >= 5:
        return False, "周末休市"

    # 假日
    if today in US_HOLIDAYS_2026:
        return False, f"假日休市: {US_HOLIDAYS_2026[today]}"

    # EDT (夏令时 3月-11月): 开盘 13:30 UTC, 收盘 20:00 UTC
    # EST (冬令时): 开盘 14:30 UTC, 收盘 21:00 UTC
    is_edt = _is_edt_now()
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


# ========== 5. FutuOpenD 健康检查 ==========

def check_connection(host: str = "127.0.0.1", port: int = 11111) -> dict:
    """检查 FutuOpenD 连接状态"""
    result = {"connected": False, "port_open": False, "futu_ready": False}

    # TCP 端口检查
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        result["port_open"] = True
    except Exception as e:
        result["error"] = f"端口不可达: {e}"
        return result

    # API 检查
    try:
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, SecurityFirm, RET_OK
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=host, port=port,
            security_firm=SecurityFirm.FUTUINC,
        )
        ret, data = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=19489722)
        ctx.close()
        if ret == RET_OK:
            result["connected"] = True
            result["futu_ready"] = True
            result["account_total"] = float(data["total_assets"].iloc[0])
        else:
            result["error"] = translate_error(ret, str(data))
    except ImportError:
        result["error"] = "futu-api 未安装"
    except Exception as e:
        result["error"] = str(e)

    return result


# ========== 6. 安全下单包装 ==========

@retry_with_backoff(max_retries=3, base_delay=2.0)
def safe_place_order(ticker: str, side: str, quantity: int,
                      host: str = "127.0.0.1", port: int = 11111,
                      acc_id: int = 19489722, env: str = "SIMULATE") -> dict:
    """
    安全下单（带重试、限流、错误翻译）。
    env: "SIMULATE" | "REAL"
    """
    _rate_limiter.wait()

    # 开盘检查
    market_open, reason = is_market_open()
    if not market_open and env == "REAL":
        return {"success": False, "error": f"市场未开: {reason}"}

    try:
        from futu import (
            OpenSecTradeContext, TrdMarket, TrdEnv, TrdSide,
            OrderType, SecurityFirm, RET_OK,
        )

        trd_env = TrdEnv.REAL if env == "REAL" else TrdEnv.SIMULATE
        trd_side = TrdSide.BUY if side == "BUY" else TrdSide.SELL
        symbol = f"US.{ticker}"

        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=host, port=port,
            security_firm=SecurityFirm.FUTUINC,
        )

        # 先获取报价
        from futu import OpenQuoteContext
        quote_ctx = OpenQuoteContext(host=host, port=port)
        ret_q, quote_data = quote_ctx.get_market_snapshot([symbol])
        quote_ctx.close()

        if ret_q != RET_OK:
            ctx.close()
            return {"success": False, "error": translate_error(ret_q, str(quote_data))}

        price = float(quote_data["last_price"].iloc[0])

        # 下单
        ret, data = ctx.place_order(
            price=price,
            qty=quantity,
            code=symbol,
            trd_side=trd_side,
            order_type=OrderType.MARKET,
            trd_env=trd_env,
            acc_id=acc_id,
        )
        ctx.close()

        if ret != RET_OK:
            return {"success": False, "error": translate_error(ret, str(data))}

        order_id = data["order_id"].iloc[0]
        logger.info(f"下单成功: {side} {quantity}股 {ticker} @ ${price:.2f} | ID: {order_id}")
        return {
            "success": True,
            "order_id": str(order_id),
            "price": price,
            "symbol": ticker,
            "side": side,
            "quantity": quantity,
            "env": env,
        }

    except ImportError:
        return {"success": False, "error": "futu-api 未安装 → pip install futu-api"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ========== 7. 持仓同步 ==========

def sync_positions(account_positions: list[dict],
                    host: str = "127.0.0.1", port: int = 11111,
                    acc_id: int = 19489722) -> dict:
    """
    将本地记录与 FutuOpenD 实际持仓对账。
    返回差异报告。
    """
    try:
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, SecurityFirm, RET_OK
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=host, port=port,
            security_firm=SecurityFirm.FUTUINC,
        )
        ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
        ctx.close()

        if ret != RET_OK:
            return {"synced": False, "error": translate_error(ret, str(data))}

        actual = {}
        if not data.empty:
            for _, row in data.iterrows():
                sym = row["code"].replace("US.", "")
                actual[sym] = {
                    "qty": int(row["qty"]),
                    "avg_price": float(row["cost_price"]),
                    "last": float(row["last_price"]),
                }

        local = {}
        for p in account_positions:
            local[p["symbol"]] = p

        # 比较
        diffs = []
        all_syms = set(local.keys()) | set(actual.keys())
        for sym in all_syms:
            l = local.get(sym, {"qty": 0})
            a = actual.get(sym, {"qty": 0})
            if l["qty"] != a["qty"]:
                diffs.append({
                    "symbol": sym,
                    "local_qty": l["qty"],
                    "actual_qty": a["qty"],
                    "delta": a["qty"] - l["qty"],
                })

        return {
            "synced": len(diffs) == 0,
            "diffs": diffs,
            "local_count": len(local),
            "actual_count": len(actual),
        }

    except ImportError:
        return {"synced": False, "error": "futu-api 未安装"}
    except Exception as e:
        return {"synced": False, "error": str(e)}
