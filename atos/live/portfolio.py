"""ATOS PRO v2 — 账户状态获取（带 futu 降级）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Bug #1: futu 模块级 import → 无 FutuOpenD 就全局 crash。
# 改为按需 import + try/except，回退到模拟账户。
_FUTU_AVAILABLE = False
try:
    from futu import OpenSecTradeContext, TrdMarket, TrdEnv, SecurityFirm, RET_OK
    _FUTU_AVAILABLE = True
except ImportError:
    pass

HOST   = "127.0.0.1"
PORT   = 11111
ACC_ID = 19489722

# 资金分级：越小越激进
VERY_SMALL_THRESHOLD  = 50_000
AGGRESSIVE_THRESHOLD  = 200_000
MODERATE_THRESHOLD    = 500_000

VERY_AGGRESSIVE = {"short_pct": 0.00, "long_pct": 0.95, "cash_pct": 0.05, "max_positions": 3,  "max_single_pct": 0.25}
AGGRESSIVE      = {"short_pct": 0.20, "long_pct": 0.70, "cash_pct": 0.10, "max_positions": 5,  "max_single_pct": 0.20}
MODERATE        = {"short_pct": 0.30, "long_pct": 0.60, "cash_pct": 0.10, "max_positions": 8,  "max_single_pct": 0.15}
CONSERVATIVE    = {"short_pct": 0.20, "long_pct": 0.70, "cash_pct": 0.05, "max_positions": 10, "max_single_pct": 0.15}

# 模拟账户默认值（fallback 用）
_SIMULATED_ACCOUNT = {
    "total": 1_000_000.0,
    "cash": 800_000.0,
    "mkt_val": 200_000.0,
    "mode": "MODERATE",
    "alloc": MODERATE,
    "positions": [
        {"symbol": "SPY", "qty": 100, "avg_price": 500.0, "last": 520.0, "mkt_val": 52000.0, "pnl_pct": 0.04},
    ],
    "constraints": {
        "max_single_pct": 0.15,
        "short_budget": 300_000.0,
        "long_budget": 600_000.0,
        "min_cash": 100_000.0,
    },
}

def get_tier(total: float) -> str:
    if total < VERY_SMALL_THRESHOLD:  return "VERY_AGGRESSIVE"
    elif total < AGGRESSIVE_THRESHOLD: return "AGGRESSIVE"
    elif total < MODERATE_THRESHOLD:   return "MODERATE"
    else:                              return "CONSERVATIVE"

def get_alloc(total: float) -> dict:
    tier = get_tier(total)
    return {"VERY_AGGRESSIVE": VERY_AGGRESSIVE, "AGGRESSIVE": AGGRESSIVE,
            "MODERATE": MODERATE, "CONSERVATIVE": CONSERVATIVE}[tier]

def get_account_state():
    """获取真实账户状态。无 FutuOpenD 时回退到模拟账户。"""
    if not _FUTU_AVAILABLE:
        return dict(_SIMULATED_ACCOUNT)

    try:
        ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=HOST, port=PORT,
                                  security_firm=SecurityFirm.FUTUINC)
        ret_acc, acc = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        ret_pos, pos = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        ctx.close()
    except Exception as e:
        # FutuOpenD 未启动或网络断开 → 回退模拟账户
        import logging
        logging.getLogger("portfolio").warning(f"FutuOpenD 不可用，回退模拟账户: {e}")
        return dict(_SIMULATED_ACCOUNT)

    if ret_acc != RET_OK:
        return dict(_SIMULATED_ACCOUNT)

    total  = float(acc["total_assets"].iloc[0])
    cash   = float(acc["cash"].iloc[0])
    mktval = float(acc["market_val"].iloc[0])
    alloc  = get_alloc(total)
    mode   = get_tier(total)
    positions = []
    if ret_pos == RET_OK and not pos.empty:
        for _, row in pos.iterrows():
            positions.append({
                "symbol":    row["code"].replace("US.", ""),
                "qty":       int(row["qty"]),
                "avg_price": float(row["cost_price"]),
                "last":      float(row["last_price"]),
                "mkt_val":   float(row.get("market_val", 0)),
                "pnl_pct":   float(row.get("pl_ratio", 0)),
            })
    return {
        "total":     total, "cash": cash, "mkt_val": mktval,
        "mode":      mode,  "alloc": alloc, "positions": positions,
        "constraints": {
            "max_single_pct": alloc.get("max_single_pct", 0.20),
            "short_budget":   total * alloc["short_pct"],
            "long_budget":    total * alloc["long_pct"],
            "min_cash":       total * alloc["cash_pct"],
        },
    }
