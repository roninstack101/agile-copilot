"""
Scheduler — runs daily notifications at fixed times (IST).

  9:00 AM IST        → Missing EOD check (who didn't submit yesterday)
  9:30 AM IST        → Morning todo summary
  10:15 AM IST       → Agile update reminder
  11:30 AM IST       → Progress report
  6:00 PM IST        → EOD reminder
  9:00 AM on 1st     → Monthly EOD calendar (previous month)
"""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone


def _is_off_day(dt: datetime) -> bool:
    """Return True if notifications should NOT be sent on this day.

    Off days:
    - All Sundays (weekday == 6)
    - 1st Saturday of the month (weekday == 5, day <= 7)
    - 3rd Saturday of the month (weekday == 5, 15 < day <= 21)
    """
    if dt.weekday() == 6:  # Sunday
        return True
    if dt.weekday() == 5:  # Saturday
        if dt.day <= 7:  # 1st Saturday
            return True
        if 15 < dt.day <= 21:  # 3rd Saturday
            return True
    return False

logger = logging.getLogger(__name__)

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

MISSING_EOD_TIME  = time(9, 0)      # 9:00 AM IST
TODO_SUMMARY_TIME = time(9, 30)     # 9:30 AM IST
MORNING_SUMMARY_TIME = time(10, 15) # 10:15 AM IST
PROGRESS_REPORT_TIME = time(11, 30) # 11:30 AM IST
EOD_REMINDER_TIME = time(18, 0)     # 6:00 PM IST


class Scheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False

    async def _loop(self, eod_callback, morning_callback, progress_callback,
                    todo_callback, missing_eod_callback, monthly_calendar_callback):
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

                # Monthly EOD calendar — fires on the 1st of each month at 9:00 AM
                # Checked before the off-day skip so it fires regardless of day of week
                monthly_key = f"monthly_{today_key}"
                if (
                    monthly_key not in fired_today
                    and now.day == 1
                    and now.time() >= MISSING_EOD_TIME
                    and now.time() < time(9, 30)
                ):
                    fired_today.add(monthly_key)
                    logger.info("Triggering monthly EOD calendar")
                    try:
                        await monthly_calendar_callback()
                    except Exception as e:
                        logger.error("Monthly EOD calendar failed: %s", e)

                # Skip remaining notifications on Sundays, 1st and 3rd Saturdays
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

                # 9:30 AM todo summary
                todo_key = f"todo_{today_key}"
                if (
                    todo_key not in fired_today
                    and now.time() >= TODO_SUMMARY_TIME
                    and now.time() < time(10, 0)
                ):
                    fired_today.add(todo_key)
                    logger.info("Triggering todo summary")
                    try:
                        await todo_callback()
                    except Exception as e:
                        logger.error("Todo summary failed: %s", e)

                # 10:15 AM agile update reminder
                morning_key = f"morning_{today_key}"
                if (
                    morning_key not in fired_today
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
                    progress_key not in fired_today
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
                    eod_key not in fired_today
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
              todo_callback, missing_eod_callback, monthly_calendar_callback):
        """Start the scheduler loop with the given async callbacks."""
        self._running = True
        self._task = asyncio.create_task(
            self._loop(eod_callback, morning_callback, progress_callback,
                       todo_callback, missing_eod_callback, monthly_calendar_callback)
        )
        logger.info(
            "Scheduler started (missing EOD @ 9AM, todo @ 9:30AM, agile reminder @ 10:15AM, "
            "progress @ 11:30AM, EOD @ 6PM IST, monthly calendar on 1st)"
        )

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("Scheduler stopped")


scheduler = Scheduler()
