"""
Scheduler — runs daily notifications at fixed times (IST).

  9:00 AM IST        → Missing EOD check (who didn't submit yesterday) — "EOD Status"
  4:00 PM IST        → Social-media reminder (runs every day, including weekends —
                        the content calendar itself has weekend posts)

  Agile update reminder (10:15 AM), progress report (11:30 AM), and the 6 PM
  EOD reminder are disabled — pass their callbacks as None to re-enable.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone


def _is_off_day(dt: datetime) -> bool:
    """Return True if notifications should NOT be sent on this day.

    Off days: all Saturdays and Sundays.
    """
    return dt.weekday() in (5, 6)  # Saturday = 5, Sunday = 6

logger = logging.getLogger(__name__)

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

MISSING_EOD_TIME  = time(9, 0)      # 9:00 AM IST
TODO_SUMMARY_TIME = time(9, 30)     # 9:30 AM IST
MORNING_SUMMARY_TIME = time(10, 15) # 10:15 AM IST
PROGRESS_REPORT_TIME = time(11, 30) # 11:30 AM IST
SOCIAL_REMINDER_TIME = time(16, 0)  # 4:00 PM IST
EOD_REMINDER_TIME = time(18, 0)     # 6:00 PM IST


class Scheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False

    async def _loop(self, eod_callback, morning_callback, progress_callback,
                    missing_eod_callback, social_callback=None):
        """Main loop — checks time every 30 seconds, fires callbacks at target times."""
        last_date: str = ""
        fired_today: set[str] = set()

        while self._running:
            try:
                now = datetime.now(IST)
                today_key = now.strftime("%Y-%m-%d")

                # Reset fired set when date changes
                if today_key != last_date:
                    fired_today = set()
                    last_date = today_key
                    logger.info("New day detected: %s — reset fired markers", today_key)

                # Social-media reminder runs every calendar day, including weekends.
                social_key = f"social_{today_key}"
                if (
                    social_callback is not None
                    and social_key not in fired_today
                    and now.time() >= SOCIAL_REMINDER_TIME
                    and now.time() < time(16, 30)
                ):
                    fired_today.add(social_key)
                    logger.info("Triggering social-media reminder")
                    try:
                        await social_callback()
                    except Exception as e:
                        logger.error("Social-media reminder failed: %s", e)

                # Existing Agile notifications do not run on weekends.
                if _is_off_day(now):
                    await asyncio.sleep(30)
                    continue

                # 9:00 AM missing EOD check (reports who missed yesterday's EOD)
                missing_eod_key = f"missing_eod_{today_key}"
                if (
                    missing_eod_key not in fired_today
                    and now.time() >= MISSING_EOD_TIME
                    and now.time() < time(9, 30)
                ):
                    fired_today.add(missing_eod_key)
                    logger.info("Triggering missing EOD check")
                    try:
                        await missing_eod_callback()
                    except Exception as e:
                        logger.error("Missing EOD check failed: %s", e)

                # 10:15 AM agile update reminder
                morning_key = f"morning_{today_key}"
                if (
                    morning_callback is not None
                    and morning_key not in fired_today
                    and now.time() >= MORNING_SUMMARY_TIME
                    and now.time() < time(10, 45)
                ):
                    fired_today.add(morning_key)
                    logger.info("Triggering agile update reminder")
                    try:
                        await morning_callback()
                    except Exception as e:
                        logger.error("Agile update reminder failed: %s", e)

                # 11:30 AM progress report
                progress_key = f"progress_{today_key}"
                if (
                    progress_callback is not None
                    and progress_key not in fired_today
                    and now.time() >= PROGRESS_REPORT_TIME
                    and now.time() < time(12, 0)
                ):
                    fired_today.add(progress_key)
                    logger.info("Triggering progress report")
                    try:
                        await progress_callback()
                    except Exception as e:
                        logger.error("Progress report failed: %s", e)

                # 6:00 PM EOD reminder
                eod_key = f"eod_{today_key}"
                if (
                    eod_callback is not None
                    and eod_key not in fired_today
                    and now.time() >= EOD_REMINDER_TIME
                    and now.time() < time(18, 30)
                ):
                    fired_today.add(eod_key)
                    logger.info("Triggering EOD reminder")
                    try:
                        await eod_callback()
                    except Exception as e:
                        logger.error("EOD reminder failed: %s", e)

                await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.info("Scheduler loop cancelled")
                break
            except Exception as e:
                logger.error("Scheduler error: %s", e)
                await asyncio.sleep(60)

    def start(self, eod_callback, morning_callback, progress_callback,
              missing_eod_callback, social_callback=None):
        """Start the scheduler loop with the given async callbacks."""
        self._running = True
        self._task = asyncio.create_task(
            self._loop(eod_callback, morning_callback, progress_callback,
                       missing_eod_callback, social_callback)
        )
        active = []
        if missing_eod_callback is not None:
            active.append("missing EOD @ 9AM")
        if morning_callback is not None:
            active.append("agile reminder @ 10:15AM")
        if progress_callback is not None:
            active.append("progress @ 11:30AM")
        if social_callback is not None:
            active.append("social @ 4PM")
        if eod_callback is not None:
            active.append("EOD reminder @ 6PM")
        logger.info("Scheduler started (%s IST)", ", ".join(active) or "nothing enabled")

    @property
    def is_running(self) -> bool:
        return bool(self._running and self._task and not self._task.done())

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("Scheduler stopped")


scheduler = Scheduler()
