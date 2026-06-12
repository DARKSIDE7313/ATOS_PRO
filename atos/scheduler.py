"""
ATOS Scheduler — APScheduler-powered job orchestration.
Hosts Vibe-Trading jobs (morning scan, shadow review, alpha bench)
and any future scheduled tasks for the ATOS PRO system.

Usage:
    from atos.scheduler import start_scheduler, signal_queue
    await start_scheduler()
    # signal_queue.get() to consume Vibe signals in your main loop
"""

import asyncio
import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from atos.layers import register_vibe_jobs
from atos.live.kelly import kelly_fraction

logger = logging.getLogger("atos.scheduler")

# --- Shared state ---
scheduler: AsyncIOScheduler | None = None
signal_queue: asyncio.Queue = asyncio.Queue(maxsize=100)


# --- Kelly wrapper for Vibe adapter ---
# kelly_fraction(ticker, confidence) → position size fraction (0.0-0.15)
def vibe_kelly(ticker: str, confidence: float) -> float:
    """Kelly sizing for Vibe signals. Confidence modulates final size."""
    base = kelly_fraction()
    return base * confidence


# --- Trades CSV path (for shadow_review) ---
def get_today_trades_csv() -> str:
    """Return the path to today's trade CSV export (Futu format)."""
    today = datetime.now().strftime("%Y%m%d")
    data_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    return os.path.join(data_dir, f"trades_{today}.csv")


# --- Alpha config updater ---
def update_alpha_config(alive_factors: list[dict]) -> None:
    """
    Callback for weekly alpha bench results.
    Saves the surviving (alive) factors to a JSON config
    that the factor engine can reference.
    """
    import json

    data_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "vibe_alive_factors.json")
    payload = {
        "updated_at": datetime.now().isoformat(),
        "alive_count": len(alive_factors),
        "factors": alive_factors,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info(
        "[Scheduler] Saved %d alive factors → %s", len(alive_factors), path
    )


async def start_scheduler() -> AsyncIOScheduler:
    """Start the APScheduler with all Vibe jobs registered."""
    global scheduler

    if scheduler is not None:
        logger.warning("[Scheduler] Already running, skipping.")
        return scheduler

    scheduler = AsyncIOScheduler()
    scheduler.start()

    register_vibe_jobs(
        scheduler=scheduler,
        signal_queue_put=signal_queue.put,
        kelly_fn=vibe_kelly,
        trades_csv_path_fn=get_today_trades_csv,
        update_alpha_config_fn=update_alpha_config,
    )

    logger.info("[Scheduler] Started with Vibe jobs registered.")
    return scheduler


async def stop_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("[Scheduler] Stopped.")
