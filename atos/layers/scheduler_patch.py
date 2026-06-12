"""scheduler_patch.py — Vibe 排程任務注冊"""
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from .vibe_bridge import VibeBridge, load_cached_signals
from .signal_adapter import SignalAdapter

logger = logging.getLogger("atos.scheduler_patch")
_bridge = VibeBridge()
_adapter = SignalAdapter(min_confidence=0.55, max_signals=10)

HK_UNIVERSE = [
    "0700.HK",
    "9988.HK",
    "3690.HK",
    "2318.HK",
    "1810.HK",
    "0941.HK",
    "1024.HK",
    "9618.HK",
]


async def _morning_scan_job(signal_queue_put, kelly_fn):
    online = await _bridge.healthcheck()
    signals = (
        await _bridge.morning_scan(HK_UNIVERSE)
        if online
        else load_cached_signals()
    )
    if not signals:
        return
    adapted = _adapter.adapt(signals, kelly_fn)
    for item in adapted:
        try:
            await signal_queue_put(item)
        except Exception as e:
            logger.error(
                "[Scheduler] Queue push failed %s: %s", item["ticker"], e
            )
    logger.info("[Scheduler] morning_scan pushed %d signals", len(adapted))


async def _eod_shadow_job(trades_csv_path_fn):
    if not await _bridge.healthcheck():
        return
    try:
        csv = trades_csv_path_fn()
        report = await _bridge.shadow_review(csv)
        if report:
            logger.info("[Scheduler] Shadow report: %s", report)
    except Exception as e:
        logger.error("[Scheduler] shadow_review error: %s", e)


async def _weekly_alpha_bench_job(update_alpha_config_fn):
    if not await _bridge.healthcheck():
        return
    alive = await _bridge.alpha_bench_hk(HK_UNIVERSE)
    if alive:
        try:
            update_alpha_config_fn(alive)
        except Exception as e:
            logger.error("[Scheduler] alpha config update failed: %s", e)


def register_vibe_jobs(
    scheduler: AsyncIOScheduler,
    signal_queue_put,
    kelly_fn,
    trades_csv_path_fn,
    update_alpha_config_fn,
    timezone: str = "Asia/Hong_Kong",
):
    """一次性注冊所有 Vibe 相關的定時任務到現有 scheduler"""
    scheduler.add_job(
        lambda: asyncio.ensure_future(
            _morning_scan_job(signal_queue_put, kelly_fn)
        ),
        CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=timezone),
        id="vibe_morning_scan",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(_eod_shadow_job(trades_csv_path_fn)),
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=timezone),
        id="vibe_shadow_review",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(
            _weekly_alpha_bench_job(update_alpha_config_fn)
        ),
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=timezone),
        id="vibe_alpha_bench",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    logger.info("[Scheduler] Vibe jobs registered.")
