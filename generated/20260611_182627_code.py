import numpy as np
from typing import Sequence, Union


def calculate_sharpe_ratio(
    returns: Union[Sequence[float], np.ndarray],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Calculate annualized Sharpe ratio.

    Args:
        returns: Sequence of periodic returns (e.g. daily).
        risk_free_rate: Annual risk-free rate (default 0.0).
        periods_per_year: Trading periods per year (default 252).

    Returns:
        Annualized Sharpe ratio.

    Raises:
        ValueError: If returns is empty, contains NaN/inf, or has zero std.
        TypeError: If inputs have incorrect types.
    """
    if not isinstance(returns, (list, tuple, np.ndarray)):
        raise TypeError("returns must be a Sequence[float]")
    if isinstance(risk_free_rate, bool) or isinstance(periods_per_year, bool):
        raise TypeError("risk_free_rate and periods_per_year must not be bool")
    if not isinstance(risk_free_rate, (int, float)) or not isinstance(periods_per_year, int):
        raise TypeError("risk_free_rate must be numeric and periods_per_year must be int")

    if len(returns) < 2:
        raise ValueError("returns must have at least 2 elements for sample standard deviation")

    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be a positive integer")

    try:
        arr = np.asarray(returns, dtype=np.float64)
    except (ValueError, TypeError) as e:
        raise TypeError(f"returns must contain numeric values: {e}") from e

    if np.any(~np.isfinite(arr)):
        raise ValueError("returns contain NaN or infinite values")

    rf_per_period = risk_free_rate / periods_per_year
    excess = arr - rf_per_period
    std = np.std(excess, ddof=1)

    if not np.isfinite(std) or std < 1e-10:
        raise ValueError("standard deviation of excess returns is zero or undefined")

    sharpe = (np.mean(excess) / std) * np.sqrt(periods_per_year)
    return float(sharpe)