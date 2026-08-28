"""
Kelly Criterion position sizing module.
Uses half-Kelly for safety. Win rate and win/loss ratio
are loaded from trade_stats.json and updated after every trade.

v6 Fixes:
  #3: Kelly correlation penalty linear→sqrt(N)
  #6: save_trade hardened with retry + atomic write + logging
  #12: kelly_after_drawdown integrated into kelly_fraction()
"""

import json
import os
import math
import time
from atos.core.logging import get_logger

logger = get_logger("kelly")

STATS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "trade_stats.json"
)

DEFAULT_WIN_RATE   = 0.50   # v23: 匹配实际胜率 (76%→保守取50%, 原0.35太悲观)
DEFAULT_WIN_LOSS_R = 1.50   # v23: 匹配实际盈亏比 (2.81→保守取1.5, 原1.00太悲观)
MIN_TRADES_FOR_LIVE_STATS = 20
MIN_TRADES_FOR_PARTIAL = 5
HALF_KELLY = 0.40           # v23: 从0.35提高到0.40 — 实际胜率高,可以更积极
MAX_KELLY_PCT = 0.12        # v23: 从0.10提高到0.12 — 高胜率时允许更大仓位
MAX_SAVE_RETRIES = 3
RECENCY_WEIGHT = 0.85       # 🆕 近期加权系数：最近交易权重 85%，历史交易 15%
MAX_USD_PF = 999.0          # P1-1: 无亏损交易时 PF 上界（避免 JSON inf）
BOOTSTRAP_MAX_ABS = 0.02    # P1-2: bootstrap 合成值的最大绝对值（真实交易盈亏幅度更大）


def _load_stats():
    if not os.path.exists(STATS_PATH):
        return None
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        return None
    # P1-2: 清除 bootstrap 残留污染（幂等：清洗后回写一次，重复加载不再剔除）
    cleaned, changed = _clean_bootstrap_stats(stats)
    if changed:
        try:
            _persist_stats(cleaned)
        except Exception as e:
            logger.error(f"[kelly] P1-2: bootstrap 清洗回写失败: {e}")
    return cleaned


def _compute_usd_stats(usd_trades: list) -> dict:
    """P1-1: 由美元 PnL 序列计算美元口径统计。

    返回 {} 当无美元数据时（向后兼容降级），调用方据此回退百分比逻辑。
    美元 profit factor = 总盈利 / 总亏损（真实盈亏，不受仓位百分比扭曲）。
    """
    if not usd_trades:
        return {}
    wins = [t for t in usd_trades if t > 0]
    losses = [t for t in usd_trades if t < 0]
    total = len(usd_trades)

    usd_win_rate = len(wins) / total
    avg_win_usd = sum(wins) / len(wins) if wins else 0.0
    avg_loss_usd = abs(sum(losses) / len(losses)) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    usd_pf = (gross_profit / gross_loss if gross_loss > 0
              else (MAX_USD_PF if gross_profit > 0 else 0.0))

    return {
        "usd_win_rate": round(usd_win_rate, 4),
        "usd_profit_factor": round(usd_pf, 4),
        "avg_win_usd": round(avg_win_usd, 2),
        "avg_loss_usd": round(avg_loss_usd, 2),
    }


def _recompute_derived_stats(stats: dict) -> dict:
    """P1-2: 由 trades / trades_usd 重算派生统计字段。

    与 save_trade 共用同一口径，避免「剔除 bootstrap 后」与「新增交易后」
    两处计算逻辑漂移。空 trades 时回退默认值，不崩溃。
    """
    trades = stats.get("trades", [])
    if not isinstance(trades, list):
        trades = []
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    stats["trades"] = trades
    stats["total_trades"] = len(trades)
    stats["win_rate"] = len(wins) / len(trades) if trades else DEFAULT_WIN_RATE
    stats["avg_win"] = sum(wins) / len(wins) if wins else 0.15
    stats["avg_loss"] = abs(sum(losses) / len(losses)) if losses else 0.05
    stats["win_loss_r"] = (stats["avg_win"] / stats["avg_loss"]
                           if stats["avg_loss"] > 0 else DEFAULT_WIN_LOSS_R)
    # P1-1: 美元口径（无 trades_usd 时返回 {} → 不写美元字段，优雅降级）
    usd_stats = _compute_usd_stats(stats.get("trades_usd", []))
    if usd_stats:
        stats.update(usd_stats)
    return stats


def _persist_stats(stats: dict) -> None:
    """原子写回 stats 到 trade_stats.json（tmp → rename，防中途截断）。"""
    os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    tmp_path = STATS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    os.replace(tmp_path, STATS_PATH)


def _strip_bootstrap_prefix(trades: list) -> list:
    """P1-2: 剔除 trades 数组前导的合成 bootstrap 小值。

    只剥离前导连续小值（|v| <= BOOTSTRAP_MAX_ABS），遇到第一个真实交易即停止，
    避免误删后续真实的小额交易（如止损离场的小亏损）。全为 bootstrap 时返回空列表。
    """
    idx = 0
    for v in trades:
        if isinstance(v, (int, float)) and abs(v) <= BOOTSTRAP_MAX_ABS:
            idx += 1
        else:
            break
    return trades[idx:]


def _clean_bootstrap_stats(stats: dict):
    """P1-2: 清除 trade_stats.json 的 bootstrap 合成残留。

    检测信号：_note 含 'Bootstrap' 或存在 _reset_at（bootstrap 种入标记）。
    命中后剔除 trades 前导合成小值、同步剔除 trades_usd/trades_weight 平行数组、
    重算派生统计，并移除 _note/_reset_at 标记 + 写入 _bootstrap_stripped 实现幂等。
    返回 (stats, changed)。
    """
    if not isinstance(stats, dict):
        return stats, False
    if stats.get("_bootstrap_stripped"):
        return stats, False
    note = str(stats.get("_note", ""))
    contaminated = ("bootstrap" in note.lower()) or bool(stats.get("_reset_at"))
    if not contaminated:
        return stats, False

    trades = stats.get("trades")
    if not isinstance(trades, list) or not trades:
        # 无 trades 数据：仅清除残留标记，幂等
        stats.pop("_note", None)
        stats.pop("_reset_at", None)
        stats["_bootstrap_stripped"] = True
        return stats, True

    stripped = _strip_bootstrap_prefix(trades)
    removed = len(trades) - len(stripped)
    stats["trades"] = stripped

    # 平行数组（美元/权重口径）同步剔除同样前导条目
    for key in ("trades_usd", "trades_weight"):
        arr = stats.get(key)
        if isinstance(arr, list) and len(arr) == len(trades):
            stats[key] = arr[removed:]

    # 重算 total_trades/win_rate/avg_win/avg_loss（及美元口径字段）
    _recompute_derived_stats(stats)

    stats.pop("_note", None)
    stats.pop("_reset_at", None)
    stats["_bootstrap_stripped"] = True
    if removed > 0:
        stats["_bootstrap_removed"] = removed
        logger.warning(f"[kelly] P1-2: 剔除 {removed} 条 bootstrap 合成交易, "
                       f"剩余 {len(stripped)} 条真实交易")
    return stats, True


def _weighted_pnl_metrics(values: list):
    """P1-1: 对任意 PnL 序列做近期加权，返回胜率/平均盈亏/profit factor。

    供 _recency_weighted_stats 复用，使美元口径（trades_usd）享受与百分比
    口径一致的近期加权，避免 Kelly 只看旧数据。数据不足（<3 笔）返回 None。
    """
    if not values or len(values) < 3:
        return None
    recent_n = min(len(values), 20)
    recent = values[-recent_n:]
    weights = [RECENCY_WEIGHT ** (recent_n - i - 1) for i in range(recent_n)]
    total_weight = sum(weights)

    wins = []
    losses = []
    for i, v in enumerate(recent):
        w = weights[i]
        if v > 0:
            wins.append((v, w))
        elif v < 0:
            losses.append((abs(v), w))

    if not wins and not losses:
        return None

    win_weight = sum(w for _, w in wins)
    win_rate = win_weight / total_weight if total_weight > 0 else DEFAULT_WIN_RATE
    avg_win = sum(v * w for v, w in wins) / sum(w for _, w in wins) if wins else 0.0
    avg_loss = sum(v * w for v, w in losses) / sum(w for _, w in losses) if losses else 0.0
    gross_profit = sum(v * w for v, w in wins)
    gross_loss = sum(v * w for v, w in losses)
    pf = (gross_profit / gross_loss if gross_loss > 0
          else (MAX_USD_PF if gross_profit > 0 else 0.0))

    return {
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": pf,
    }


def _recency_weighted_stats(stats: dict) -> dict:
    """计算近期加权的胜率和盈亏比。

    最近 N 笔交易权重更高（RECENCY_WEIGHT=85%），
    避免旧数据（可能来自不同市场环境）误导当前决策。
    文艺复兴/AQR等机构基金都使用类似的指数衰减加权。
    """
    trades = stats.get("trades", [])
    if not trades or len(trades) < 3:
        return stats

    total_trades = len(trades)
    # 只取最近 20 笔交易做加权
    recent_n = min(total_trades, 20)
    recent_trades = trades[-recent_n:]

    # 指数衰减权重: 最近交易权重最大
    weights = []
    for i in range(recent_n):
        # 指数衰减: w_i = alpha^(recent_n - i - 1)
        w = RECENCY_WEIGHT ** (recent_n - i - 1)
        weights.append(w)

    total_weight = sum(weights)

    wins = []
    losses = []
    for i, pnl in enumerate(recent_trades):
        w = weights[i]
        if pnl > 0:
            wins.append((pnl, w))
        elif pnl < 0:
            losses.append((abs(pnl), w))

    if not wins and not losses:
        return stats

    # 加权胜率
    win_weight = sum(w for _, w in wins)
    loss_weight = sum(w for _, w in losses)
    weighted_wr = win_weight / total_weight if total_weight > 0 else DEFAULT_WIN_RATE

    # 加权平均盈亏比
    if wins and losses:
        weighted_avg_win = sum(p * w for p, w in wins) / sum(w for _, w in wins) if wins else 0.01
        weighted_avg_loss = sum(p * w for p, w in losses) / sum(w for _, w in losses) if losses else 0.01
        weighted_wlr = weighted_avg_win / max(weighted_avg_loss, 0.001)
    else:
        weighted_wlr = DEFAULT_WIN_LOSS_R

    result = {
        "trades": trades,
        "total_trades": total_trades,
        "win_rate": round(weighted_wr, 4),
        "win_loss_r": round(weighted_wlr, 4),
        "avg_win": round(sum(p for p, _ in wins) / len(wins), 6) if wins else 0,
        "avg_loss": round(sum(p for p, _ in losses) / len(losses), 6) if losses else 0,
        "weighted": True,
        "recent_n": recent_n,
    }

    # P1-1: 美元口径近期加权（无 trades_usd 时返回 None → 优雅降级，不覆盖）
    usd_metrics = _weighted_pnl_metrics(stats.get("trades_usd", []))
    if usd_metrics is not None:
        result["usd_win_rate"] = round(usd_metrics["win_rate"], 4)
        result["usd_profit_factor"] = round(usd_metrics["profit_factor"], 4)
        result["avg_win_usd"] = round(usd_metrics["avg_win"], 2)
        result["avg_loss_usd"] = round(usd_metrics["avg_loss"], 2)

    return result


def save_trade(pnl_pct: float, pnl_usd: float = None, weight: float = None) -> dict:
    """Record a closed trade's PnL% and update running stats.

    Fix #6: atomic write + retry + logging on failure.
    Fix #14: handle old-format trade_stats.json (missing 'trades' key).

    P1-1 (统计失真): `pnl_usd` / `weight` 为可选参数，向后兼容。传入美元 PnL 时
    同步写入 trades_usd 数组（仓位权重写入 trades_weight），并额外计算美元口径
    统计字段（usd_win_rate / usd_profit_factor / avg_win_usd / avg_loss_usd），
    供 Kelly 用真实美元 profit factor 而非被仓位扭曲的百分比盈亏比做决策。
    """
    os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)

    for attempt in range(MAX_SAVE_RETRIES):
        try:
            stats = _load_stats() or {}
            # Fix #14: migrate old format or init trades list
            if "trades" not in stats:
                stats["trades"] = []
            stats["trades"].append(round(pnl_pct, 6))

            # P1-1: 记录美元口径（可选，旧调用只传 pnl_pct 时不写入）
            if pnl_usd is not None:
                stats.setdefault("trades_usd", []).append(round(pnl_usd, 2))
            if weight is not None:
                stats.setdefault("trades_weight", []).append(round(weight, 6))

            # P1-2: 重算派生统计（与 bootstrap 清洗共用同一口径）
            _recompute_derived_stats(stats)

            # Atomic write: temp file → rename
            _persist_stats(stats)

            logger.debug(f"Kelly stats updated: {len(stats['trades'])} trades, "
                         f"WR={stats['win_rate']:.2%}")
            return stats

        except Exception as e:
            logger.error(f"Kelly save_trade failed (attempt {attempt+1}/{MAX_SAVE_RETRIES}): {e}")
            if attempt < MAX_SAVE_RETRIES - 1:
                time.sleep(0.5 * (attempt + 1))

    logger.critical(f"Kelly save_trade FAILED after {MAX_SAVE_RETRIES} attempts — stats may be stale")
    return _load_stats() or {"trades": [], "win_rate": DEFAULT_WIN_RATE, "win_loss_r": DEFAULT_WIN_LOSS_R}


def crouching_allocation(score: float, drawdown: float,
                          has_news_catalyst: bool = False,
                          win_rate: float = None) -> float:
    """Crouching Method allocation v8 — win-rate 感知版

    2026-06-25 深度修复:
      - 新增 win_rate 参数：低胜率时自动缩小仓位
      - WR<0.35 → 仓位×0.5 (生存模式)
      - WR<0.25 → 仓位×0.3 (极保守)
      - 评分阈值收紧到 0.35 以上才有仓位（旧版 0.30 太松）
    """
    # 评分 → 基础仓位 (v23: 进一步提高 — 高胜率环境下积极布局)
    if score >= 0.70:
        base_pct = 0.085
    elif score >= 0.50:
        base_pct = 0.065
    elif score >= 0.40:
        base_pct = 0.050
    elif score >= 0.35:
        base_pct = 0.032
    elif score >= 0.28:
        base_pct = 0.018
    else:
        return 0.0     # <0.28 → 不开仓

    # 回撤惩罚 (v10: 回撤<5%不惩罚)
    if drawdown <= 0.05:
        dd_penalty = 1.0
    elif drawdown <= 0.10:
        dd_penalty = 1.0 - (drawdown - 0.05) / 0.05 * 0.30
    else:
        dd_penalty = max(0.50, 1.0 - (drawdown - 0.05) / 0.05 * 0.30)
    after_dd = base_pct * dd_penalty

    # 新闻催化剂加成
    if has_news_catalyst:
        after_dd *= 1.10

    # Win-rate 感知 (v10: 轻惩罚 — 只对极低胜率做保护)
    if win_rate is not None:
        if win_rate < 0.20:
            after_dd *= 0.55      # 极低 → 半仓
        elif win_rate < 0.30:
            after_dd *= 0.75      # 低 → 75%
        # WR>0.30 不惩罚 (让系统正常运转积累数据)

    final = min(after_dd, 0.12)   # v10: 硬上限 12% (旧: 10%)
    return final


def kelly_fraction(win_rate=None, win_loss_r=None, num_positions: int = 0,
                   current_drawdown: float = 0.0) -> float:
    """
    Compute half-Kelly fraction.

    Fix #3: correlation penalty uses 1/sqrt(N) instead of linear
    Fix #12: kelly_after_drawdown integrated

    Returns the recommended position size as a fraction of total equity.
    """
    stats = _load_stats()
    # 🆕 使用近期加权统计（最近的交易权重更高）
    if stats and stats.get("total_trades", 0) >= MIN_TRADES_FOR_PARTIAL:
        stats = _recency_weighted_stats(stats)

    if (stats and stats.get("total_trades", 0) >= MIN_TRADES_FOR_LIVE_STATS):
        # P1-1: 优先美元 profit factor；无美元数据时回退百分比盈亏比
        if stats.get("usd_profit_factor") is not None:
            w = stats.get("usd_win_rate", stats["win_rate"])
            r = stats["usd_profit_factor"]
            source = "live_stats_usd"
        else:
            w = stats["win_rate"]
            r = stats["win_loss_r"]
            source = "live_stats"
    elif (stats and stats.get("total_trades", 0) >= MIN_TRADES_FOR_PARTIAL):
        # 部分学习：混合 bootstrap + 实盘数据（交易数 / 20 权重）
        total = stats["total_trades"]
        blend = min(total / MIN_TRADES_FOR_LIVE_STATS, 1.0)  # e.g. 5 trades → 0.25 weight
        if stats.get("usd_profit_factor") is not None:
            live_w = stats.get("usd_win_rate", stats["win_rate"])
            live_r = stats["usd_profit_factor"]
            source = f"partial_usd({total}t)"
        else:
            live_w = stats["win_rate"]
            live_r = stats["win_loss_r"]
            source = f"partial({total}t)"
        w = blend * live_w + (1 - blend) * (win_rate or DEFAULT_WIN_RATE)
        r = blend * live_r + (1 - blend) * (win_loss_r or DEFAULT_WIN_LOSS_R)
    else:
        w = win_rate   or DEFAULT_WIN_RATE
        r = win_loss_r or DEFAULT_WIN_LOSS_R
        source = "bootstrap"

    # Kelly formula: f = W - (1-W)/R
    # P1-1: 美元 PF 可为 0（全亏无盈利单）→ 保护除零，r<=0 时停止交易
    if r and r > 0:
        full_kelly = w - (1 - w) / r
        full_kelly = max(0.0, full_kelly)
    else:
        full_kelly = 0.0
    half_kelly = full_kelly * HALF_KELLY
    capped     = min(half_kelly, MAX_KELLY_PCT)

    # Fix #3: correlation penalty → 1/sqrt(N) instead of linear
    # Rationale: diversification benefit follows sqrt(N) law
    if num_positions > 1:
        penalty_factor = 1.0 / math.sqrt(num_positions)
        penalty_factor = max(0.20, penalty_factor)  # floor at 20%
        capped *= penalty_factor
        logger.debug(f"[kelly] diversification_penalty: {num_positions} positions → ×{penalty_factor:.2f}")

    # Fix #12: drawdown-based reduction
    if current_drawdown > 0:
        from atos.risk.professional import kelly_after_drawdown
        dd_result = kelly_after_drawdown(capped, current_drawdown)
        capped = dd_result["adjusted_kelly"]
        if dd_result["scale"] < 1.0:
            logger.debug(f"[kelly] drawdown_reduction: DD={current_drawdown:.2%} → "
                        f"scale={dd_result['scale']:.0%} → kelly={capped:.4f}")

    logger.debug(f"[kelly] source={source} W={w:.2f} R={r:.2f} "
                 f"full={full_kelly:.3f} half={half_kelly:.3f} capped={capped:.3f}")
    return capped


def kelly_qty(symbol_price: float, total_equity: float,
              current_position_value: float = 0.0,
              num_positions: int = 0,
              current_drawdown: float = 0.0) -> int:
    """Return number of shares to BUY to reach Kelly-sized position."""
    target_value = total_equity * kelly_fraction(num_positions=num_positions,
                                                  current_drawdown=current_drawdown)
    delta_value  = target_value - current_position_value
    if delta_value <= 0 or symbol_price <= 0:
        return 0
    return max(1, int(delta_value / symbol_price))
