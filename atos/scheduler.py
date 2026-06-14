"""
ATOS Scheduler — APScheduler-powered job orchestration.
Runs in a background daemon thread so it works alongside
the synchronous shadow_trader main loop.

Usage:
    from atos.scheduler import start_scheduler, signal_queue

    start_scheduler()

    # In your main loop, drain Vibe signals:
    import queue
    try:
        signal = signal_queue.get_nowait()
        # process signal...
    except queue.Empty:
        pass
"""

import asyncio
import logging
import os
import queue
import threading
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from atos.layers import register_vibe_jobs
from atos.live.kelly import kelly_fraction

logger = logging.getLogger("atos.scheduler")

# --- Shared state (thread-safe) ---
signal_queue = queue.Queue(maxsize=100)
_scheduler: AsyncIOScheduler | None = None
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_shutdown_event = threading.Event()


# --- Kelly wrapper for Vibe adapter ---
def vibe_kelly(ticker: str, confidence: float) -> float:
    """Kelly sizing for Vibe signals. Confidence modulates final size."""
    base = kelly_fraction()
    return base * confidence


# --- Trades CSV path (for shadow_review) ---
def get_today_trades_csv() -> str:
    """Path to today's trade CSV export (Futu format)."""
    today = datetime.now().strftime("%Y%m%d")
    data_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    return os.path.join(data_dir, f"trades_{today}.csv")


# --- Alpha config updater ---
def update_alpha_config(alive_factors: list[dict]) -> None:
    """Save surviving alpha factors from weekly bench results."""
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


def _run_scheduler_loop() -> None:
    """Run the APScheduler event loop in this background thread."""
    global _scheduler, _loop

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    _scheduler = AsyncIOScheduler(event_loop=_loop)
    _scheduler.start()

    register_vibe_jobs(
        scheduler=_scheduler,
        signal_queue_put=lambda item: signal_queue.put(item, timeout=5) if not signal_queue.full() else logger.warning("[Scheduler] Queue full, dropping Vibe signal"),  # non-blocking
        kelly_fn=vibe_kelly,
        trades_csv_path_fn=get_today_trades_csv,
        update_alpha_config_fn=update_alpha_config,
    )

    logger.info("[Scheduler] Background thread started with Vibe jobs registered.")

    # Run until shutdown is signaled
    try:
        while not _shutdown_event.is_set():
            _loop.run_until_complete(asyncio.sleep(1))
    except Exception as e:
        logger.error("[Scheduler] Background thread error: %s", e)
    finally:
        if _scheduler:
            _scheduler.shutdown(wait=False)
        tasks = asyncio.all_tasks(_loop)
        for t in tasks:
            t.cancel()
        _loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        _loop.close()
        logger.info("[Scheduler] Background thread stopped.")


def start_scheduler() -> None:
    """Start the APScheduler in a background daemon thread."""
    global _thread

    if _thread is not None and _thread.is_alive():
        logger.warning("[Scheduler] Already running.")
        return

    _shutdown_event.clear()
    _thread = threading.Thread(target=_run_scheduler_loop, daemon=True, name="atos-scheduler")
    _thread.start()
    logger.info("[Scheduler] Launched background thread.")


def stop_scheduler() -> None:
    """Signal shutdown and wait for the background thread to finish."""
    global _thread

    if _thread is None:
        return

    _shutdown_event.set()
    _thread.join(timeout=5)
    _thread = None
