"""Optional in-process scheduler, so one deployment runs the pipeline and the weekly digest.

Enable with RUN_SCHEDULER=1. On a host that runs several web workers, use the host's own cron instead
(`python -m app.cli run`), or only one worker, so the pipeline does not run twice at the same time.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import config, digest, pipeline, telegram


def notify_admins(text: str) -> None:
    for uid in config.ADMIN_IDS:
        try:
            telegram.send_message(uid, text)
        except telegram.TelegramError as exc:
            print(f"[scheduler] could not warn admin {uid}: {exc}")


def run_pipeline_job() -> None:
    try:
        summary = pipeline.run()
        if summary["failed"]:
            notify_admins(f"Pipeline finished with {summary['failed']} failed document(s).")
    except Exception as exc:
        notify_admins(f"Pipeline run failed: {exc}")
        raise


def run_digest_job() -> None:
    try:
        sent = digest.send_weekly()
        print(f"[scheduler] weekly digest sent to {sent} users")
    except Exception as exc:
        notify_admins(f"Weekly digest failed: {exc}")
        raise


def start() -> BackgroundScheduler | None:
    if not config.RUN_SCHEDULER:
        return None
    s = BackgroundScheduler(timezone="UTC")
    s.add_job(run_pipeline_job, IntervalTrigger(hours=config.PIPELINE_INTERVAL_HOURS), id="pipeline")
    s.add_job(run_digest_job, CronTrigger(day_of_week=config.DIGEST_DAY, hour=config.DIGEST_HOUR), id="digest")
    s.start()
    print(f"[scheduler] pipeline every {config.PIPELINE_INTERVAL_HOURS}h, digest weekly")
    return s
