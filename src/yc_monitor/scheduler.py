from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from yc_monitor.pipeline import MonitorPipeline

MONITOR_JOB_ID = "monitor-cycle"
MISFIRE_GRACE_SECONDS = 3600


def build_scheduler(pipeline: MonitorPipeline, interval_hours: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        pipeline.run,
        "interval",
        hours=interval_hours,
        id=MONITOR_JOB_ID,
        max_instances=1,
        coalesce=True,
        next_run_time=None,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    return scheduler


def schedule_first_run(
    scheduler: AsyncIOScheduler,
    interval_hours: int,
    *,
    run_immediately: bool = True,
    now: datetime | None = None,
) -> datetime:
    current = now or datetime.now(UTC)
    # Delay the first cycle so FastAPI can finish startup and serve /healthz.
    next_time = (
        current + timedelta(seconds=30)
        if run_immediately
        else current + timedelta(hours=interval_hours)
    )
    scheduler.modify_job(MONITOR_JOB_ID, next_run_time=next_time)
    return next_time


def job_next_run_iso(scheduler: AsyncIOScheduler, job_id: str = MONITOR_JOB_ID) -> str | None:
    job = scheduler.get_job(job_id)
    if job is None or job.next_run_time is None:
        return None
    value = job.next_run_time
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return str(value.isoformat())
