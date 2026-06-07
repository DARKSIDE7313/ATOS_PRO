"""
Kelly Criterion position sizing module.
Uses half-Kelly for safety. Win rate and win/loss ratio
are loaded from trade_stats.json and updated after every trade.
"""
import json, os, math

STATS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "trade_stats.json"
)

# Default bootstrap values (used until enough real trades accumulate)
# Previously used W=0.70, R=3.0 which was overly optimistic — it assumed
# a 70% win rate with 3:1 reward ratio, which leads to aggressive position
# sizing (full Kelly ~70%). Switched to W=0.55, R=2.0 which is much more
# conservative:
#   - 55% win rate is realistic for systematic strategies
#   - 2:1 reward ratio is achievable without overfitting
#   - Full Kelly at these values = 55% - 45%/2 = 32.5%
#   - Half Kelly = 16.25% → well within the 15% cap below
DEFAULT_WIN_RATE   = 0.55
DEFAULT_WIN_LOSS_R = 2.0   # avg_win / avg_loss = 10% / 5%
MIN_TRADES_FOR_LIVE_STATS = 20  # use real stats only after 20 trades
HALF_KELLY = 0.5            # fraction of full Kelly to use
MAX_KELLY_PCT = 0.15        # hard cap: never bet more than 15% per position


def _load_stats():
    if not os.path.exists(STATS_PATH):
        return None
    try:
        with open(STATS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_trade(pnl_pct: float):
    """Record a closed trade's PnL% and update running stats."""
    os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    stats = _load_stats() or {"trades": []}
    stats["trades"].append(round(pnl_pct, 6))
    # Recompute aggregate stats
    trades = stats["trades"]
    wins   = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    stats["total_trades"] = len(trades)
    stats["win_rate"]     = len(wins) / len(trades) if trades else DEFAULT_WIN_RATE
    stats["avg_win"]      = sum(wins)  / len(wins)   if wins   else 0.15
    stats["avg_loss"]     = abs(sum(losses) / len(losses)) if losses else 0.05
    stats["win_loss_r"]   = (stats["avg_win"] / stats["avg_loss"]
                             if stats["avg_loss"] > 0 else DEFAULT_WIN_LOSS_R)
    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    return stats


def kelly_fraction(win_rate=None, win_loss_r=None, num_positions: int = 0) -> float:
    """
    Compute half-Kelly fraction.
    Returns the recommended position size as a fraction of total equity.

    Parameters
    ----------
    win_rate : float, optional
        Override win rate for bootstrap (default: DEFAULT_WIN_RATE)
    win_loss_r : float, optional
        Override win/loss ratio for bootstrap (default: DEFAULT_WIN_LOSS_R)
    num_positions : int
        Current number of open positions. Used for correlation penalty:
        if >3 positions, reduce Kelly by 10% per additional position over 3.
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
    full_kelly = max(0.0, full_kelly)       # can't be negative
    half_kelly = full_kelly * HALF_KELLY
    capped     = min(half_kelly, MAX_KELLY_PCT)

    # Correlation penalty: if portfolio has >3 positions, reduce Kelly
    # by 10% for each additional position over 3. This accounts for the
    # fact that more positions → higher correlation risk → smaller sizes.
    if num_positions > 3:
        penalty_factor = 1.0 - 0.10 * (num_positions - 3)
        penalty_factor = max(0.3, penalty_factor)  # floor at 30% of original
        capped *= penalty_factor
        print(f"[kelly] correlation_penalty: {num_positions} positions → ×{penalty_factor:.2f}")

    print(f"[kelly] source={source} W={w:.2f} R={r:.2f} "
          f"full={full_kelly:.3f} half={half_kelly:.3f} capped={capped:.3f}")
    return capped


def kelly_qty(symbol_price: float, total_equity: float,
              current_position_value: float = 0.0) -> int:
    """
    Return number of shares to BUY to reach Kelly-sized position.
    current_position_value: current market value already held in this symbol.
    """
    target_value = total_equity * kelly_fraction()
    delta_value  = target_value - current_position_value
    if delta_value <= 0 or symbol_price <= 0:
        return 0
    return int(delta_value / symbol_price)
