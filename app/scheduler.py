"""
Scheduler — runs daily notifications at fixed times (IST).

  6:00 PM IST  → EOD reminder to group chat
  10:00 AM IST → Morning WIP summary with AI-prioritized top 5 tasks per member

Also pings the server every 10 minutes to prevent free-tier hosts from sleeping.
"""

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

EOD_REMINDER_TIME = time(18, 0)   # 6:00 PM IST
MORNING_SUMMARY_TIME = time(10, 0)  # 10:00 AM IST


class Scheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._running = False

    async def _loop(self, eod_callback, morning_callback):
        """Main loop — checks time every 30 seconds, fires callbacks at target times."""
        fired_today: set[str] = set()

        while self._running:
            try:
                now = datetime.now(IST)
                today_key = now.strftime("%Y-%m-%d")

                # Reset fired set at midnight
                if f"eod_{today_key}" not in fired_today and f"morning_{today_key}" not in fired_today:
                    # New day — clear yesterday's markers
                    fired_today = set()

                # 6:00 PM EOD reminder
                eod_key = f"eod_{today_key}"
                if (
                    eod_key not in fired_today
                    and now.time() >= EOD_REMINDER_TIME
                    and now.time() < time(18, 5)  # 5 minute window
                ):
                    fired_today.add(eod_key)
                    logger.info("Triggering EOD reminder")
                    try:
                        await eod_callback()
                    except Exception as e:
                        logger.error("EOD reminder failed: %s", e)

                # 10:00 AM morning summary
                morning_key = f"morning_{today_key}"
                if (
                    morning_key not in fired_today
                    and now.time() >= MORNING_SUMMARY_TIME
                    and now.time() < time(10, 5)  # 5 minute window
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

    async def _keep_alive(self):
        """Ping self every 10 minutes to prevent free-tier hosts from sleeping."""
        import httpx

        render_url = os.environ.get("RENDER_EXTERNAL_URL")
        if not render_url:
            return  # Not on Render, skip

        logger.info("Keep-alive started for %s", render_url)
        while self._running:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(f"{render_url}/health")
                await asyncio.sleep(600)  # 10 minutes
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(600)

    def start(self, eod_callback, morning_callback):
        """Start the scheduler loop with the given async callbacks."""
        self._running = True
        self._task = asyncio.create_task(
            self._loop(eod_callback, morning_callback)
        )
        self._keepalive_task = asyncio.create_task(self._keep_alive())
        logger.info("Scheduler started (EOD reminder @ 6PM IST, morning summary @ 10AM IST)")

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
        if hasattr(self, "_keepalive_task") and self._keepalive_task:
            self._keepalive_task.cancel()
        logger.info("Scheduler stopped")


scheduler = Scheduler()
