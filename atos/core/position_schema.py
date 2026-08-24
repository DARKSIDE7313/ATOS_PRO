"""
ATOS PRO — 持仓 Schema 单一真源 (Phase 5 框架重塑)
====================================================
历史问题: 持仓数量键名三套并存 (shares / qty / quantity)，
靠散落各处的 ``p.get("shares", p.get("qty", ...))`` 兜底，
曾导致 execute() 裸 ["qty"] KeyError (Phase1 C2)。

收敛规则 (本模块为唯一权威):
  - 规范存储键: ``shares`` (int) — 唯一权威数量
  - 镜像键: ``qty`` — 必须与 shares 保持一致 (Dashboard/risk_manager 等
    旧读者仍读 qty，删除会破坏 dashboard/server.py 等多处)
  - ``quantity`` — 废弃。仅存在于已归档 longterm/ 代码与 Futu 订单 dict
    (不同领域)，持仓 dict 中不再出现。
  - 读取一律走 :func:`get_qty`，写入一律走 :func:`set_qty`，
    状态加载走 :func:`normalize_positions`。

持仓 dict 完整 schema (shadow_state.json → positions[sym]):
  必需: shares(int), qty(int, 镜像), avg_price(float), last_price(float)
  可选: ai_decision_id(int), buy_time(ISO8601 str), atr(float), peak_price(float)
"""

# 规范键 / 镜像键 / 废弃键
CANONICAL_KEY = "shares"
MIRROR_KEY = "qty"
LEGACY_KEYS = ("quantity",)  # 废弃，仅读取兼容

# 持仓 dict 必需字段 (契约文档，供校验/测试用)
REQUIRED_KEYS = ("shares", "qty", "avg_price", "last_price")
OPTIONAL_KEYS = ("ai_decision_id", "buy_time", "atr", "peak_price")


def get_qty(pos: dict) -> int:
    """读取持仓数量 — 统一入口 (shares → qty → quantity 回退链)"""
    if not isinstance(pos, dict):
        return 0
    qty = pos.get(CANONICAL_KEY, None)
    if qty is None:
        qty = pos.get(MIRROR_KEY, None)
    if qty is None:
        for k in LEGACY_KEYS:
            qty = pos.get(k, None)
            if qty is not None:
                break
    if qty is None:
        return 0
    try:
        return int(qty)
    except (TypeError, ValueError):
        return 0


def set_qty(pos: dict, n: int) -> None:
    """写入持仓数量 — 规范键 + 镜像键同步 (单一写入点)"""
    pos[CANONICAL_KEY] = n
    pos[MIRROR_KEY] = n
    for k in LEGACY_KEYS:
        pos.pop(k, None)


def normalize_position(pos: dict) -> dict:
    """就地标准化单个持仓 dict 的键名 (幂等)。

    逐字复刻 shadow_trader.main() 状态恢复段的原始语义:
      1. 有 shares 无 qty → qty = shares
      2. 有 qty 无 shares → shares = qty
      3. 有 quantity (且前两支未命中) → shares = qty = quantity
      4. shares/qty 俱在 → 保持原样 (不在加载时做"权威仲裁",
         写入侧由 :func:`set_qty` 保证两键同步, 使不一致状态无法产生)
    """
    if not isinstance(pos, dict):
        return pos
    if CANONICAL_KEY in pos and MIRROR_KEY not in pos:
        pos[MIRROR_KEY] = pos[CANONICAL_KEY]
    elif MIRROR_KEY in pos and CANONICAL_KEY not in pos:
        pos[CANONICAL_KEY] = pos[MIRROR_KEY]
    elif "quantity" in pos:
        pos[CANONICAL_KEY] = pos[MIRROR_KEY] = pos["quantity"]
    return pos


def normalize_positions(positions: dict) -> dict:
    """批量标准化持仓字典 (状态加载时调用一次)"""
    if not positions:
        return positions or {}
    for sym, pos in positions.items():
        normalize_position(pos)
    return positions
