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

DEFAULT_WIN_RATE   = 0.50   # 进攻性升级
DEFAULT_WIN_LOSS_R = 1.55   # 进攻性升级
MIN_TRADES_FOR_LIVE_STATS = 20
HALF_KELLY = 0.5
MAX_KELLY_PCT = 0.15
MAX_SAVE_RETRIES = 3


def _load_stats():
    if not os.path.exists(STATS_PATH):
        return None
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_trade(pnl_pct: float) -> dict:
    """Record a closed trade's PnL% and update running stats.
    Fix #6: atomic write + retry + logging on failure.
    Fix #14: handle old-format trade_stats.json (missing 'trades' key)."""
    os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)

    for attempt in range(MAX_SAVE_RETRIES):
        try:
            stats = _load_stats() or {}
            # Fix #14: migrate old format or init trades list
            if "trades" not in stats:
                # Old format — migrate any existing aggregate data
                old_trades = []
                old_total = stats.get("total_trades", 0)
                old_wins = stats.get("wins", 0)
                old_losses = stats.get("losses", 0)
                # We can't reconstruct individual trades, but we can at least
                # initialize the list with the new trade
                stats["trades"] = old_trades
            stats["trades"].append(round(pnl_pct, 6))

            trades = stats["trades"]
            wins   = [t for t in trades if t > 0]
            losses = [t for t in trades if t <= 0]
            stats["total_trades"] = len(trades)
            stats["win_rate"]     = len(wins) / len(trades) if trades else DEFAULT_WIN_RATE
            stats["avg_win"]      = sum(wins)  / len(wins)   if wins   else 0.15
            stats["avg_loss"]     = abs(sum(losses) / len(losses)) if losses else 0.05
            stats["win_loss_r"]   = (stats["avg_win"] / stats["avg_loss"]
                                     if stats["avg_loss"] > 0 else DEFAULT_WIN_LOSS_R)

            # Atomic write: temp file → rename
            tmp_path = STATS_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
            os.replace(tmp_path, STATS_PATH)

            logger.debug(f"Kelly stats updated: {len(trades)} trades, WR={stats['win_rate']:.2%}")
            return stats

        except Exception as e:
            logger.error(f"Kelly save_trade failed (attempt {attempt+1}/{MAX_SAVE_RETRIES}): {e}")
            if attempt < MAX_SAVE_RETRIES - 1:
                time.sleep(0.5 * (attempt + 1))

    logger.critical(f"Kelly save_trade FAILED after {MAX_SAVE_RETRIES} attempts — stats may be stale")
    return _load_stats() or {"trades": [], "win_rate": DEFAULT_WIN_RATE, "win_loss_r": DEFAULT_WIN_LOSS_R}


def crouching_allocation(score: float, drawdown: float,
                          has_news_catalyst: bool = False) -> float:
    """Crouching Method allocation（基金级校准版）
    
    校准原则：
    - 因子引擎新评分体系（0基准），最高分约0.40
    - 阈值从0.55降到0.30，匹配实际分数范围
    - 基础仓位放大3倍（之前2%→6%），让实际部署有意义
    """
    if score >= 0.70:
        base_pct = 0.08
    elif score >= 0.55:
        base_pct = 0.06
    elif score >= 0.40:
        base_pct = 0.05
    elif score >= 0.30:
        base_pct = 0.035
    else:
        return 0.0

    dd_penalty = max(0.0, 1.0 - (drawdown / 0.02) * 0.15)
    after_dd = base_pct * dd_penalty

    if has_news_catalyst:
        after_dd *= 1.10

    final = min(after_dd, 0.10)
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
    if (stats and stats.get("total_trades", 0) >= MIN_TRADES_FOR_LIVE_STATS):
        w = stats["win_rate"]
        r = stats["win_loss_r"]
        source = "live_stats"
    else:
        w = win_rate   or DEFAULT_WIN_RATE
        r = win_loss_r or DEFAULT_WIN_LOSS_R
        source = "bootstrap"

    # Kelly formula: f = W - (1-W)/R
    full_kelly = w - (1 - w) / r
    full_kelly = max(0.0, full_kelly)
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
