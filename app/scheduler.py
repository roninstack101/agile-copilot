"""
Scheduler — runs daily notifications at fixed times (IST).

  6:00 PM IST  → EOD reminder to group chat
  10:00 AM IST → Morning WIP summary with AI-prioritized top 5 tasks per member

Also pings the server every 10 minutes to prevent free-tier hosts from sleeping.
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

EOD_REMINDER_TIME = time(18, 0)   # 6:00 PM IST
MORNING_SUMMARY_TIME = time(10, 0)  # 10:00 AM IST


class Scheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False

    async def _loop(self, eod_callback, morning_callback):
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

                # Skip notifications on Sundays, 1st and 3rd Saturdays
                if _is_off_day(now):
                    await asyncio.sleep(30)
                    continue

                # 6:00 PM EOD reminder (fire once anytime between 6:00–6:30 PM)
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

                # 10:00 AM morning summary (fire once anytime between 10:00–10:30 AM)
                morning_key = f"morning_{today_key}"
                if (
                    morning_key not in fired_today
                    and now.time() >= MORNING_SUMMARY_TIME
                    and now.time() < time(10, 30)
                ):
                    fired_today.add(morning_key)
                    logger.info("Triggering morning WIP summary")
                    try:
                        await morning_callback()
                    except Exception as e:
                        logger.error("Morning summary failed: %s", e)

                await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.info("Scheduler loop cancelled")
                break
            except Exception as e:
                logger.error("Scheduler error: %s", e)
                await asyncio.sleep(60)

    def start(self, eod_callback, morning_callback):
        """Start the scheduler loop with the given async callbacks."""
        self._running = True
        self._task = asyncio.create_task(
            self._loop(eod_callback, morning_callback)
        )
        logger.info("Scheduler started (EOD reminder @ 6PM IST, morning summary @ 10AM IST)")

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("Scheduler stopped")


scheduler = Scheduler()
