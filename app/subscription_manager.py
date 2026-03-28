"""
Graph API subscription manager — creates and renews subscriptions
to listen for new messages in an MS Teams channel.

Includes auto-renewal: a background asyncio task that renews the
subscription every ~50 minutes so it never expires.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings, GRAPH_BASE_URL
from app.graph_auth import graph_auth

logger = logging.getLogger(__name__)

# Subscription max lifetime for channel messages is 60 minutes
SUBSCRIPTION_LIFETIME_MINUTES = 55  # renew a few minutes early


class SubscriptionManager:
    """
    Manages Microsoft Graph API subscriptions for Teams channel messages.
    Handles creation, renewal, and deletion.
    """

    def __init__(self):
        self._subscription_id: str | None = None
        self._expires_at: datetime | None = None
        self._renewal_task: asyncio.Task | None = None

    @property
    def is_active(self) -> bool:
        if not self._subscription_id or not self._expires_at:
            return False
        return datetime.now(timezone.utc) < self._expires_at

    async def create_subscription(self) -> dict:
        """
        Create a new subscription to listen for messages in the configured
        Teams channel. The Graph API will POST notifications to our webhook.
        """
        headers = await graph_auth.get_headers()

        expiration = datetime.now(timezone.utc) + timedelta(minutes=SUBSCRIPTION_LIFETIME_MINUTES)

        payload = {
            "changeType": "created",
            "notificationUrl": settings.WEBHOOK_NOTIFICATION_URL,
            "resource": f"/chats/{settings.CHAT_ID}/messages",
            "expirationDateTime": expiration.isoformat(),
            "clientState": "agile-copilot-secret",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{GRAPH_BASE_URL}/subscriptions",
                headers=headers,
                json=payload,
            )
            if not response.is_success:
                logger.error("Subscription error response: %s", response.text)
            response.raise_for_status()
            data = response.json()

        self._subscription_id = data["id"]
        self._expires_at = datetime.fromisoformat(
            data["expirationDateTime"].replace("Z", "+00:00")
        )

        logger.info(
            "Subscription created: %s, expires at %s",
            self._subscription_id,
            self._expires_at.isoformat(),
        )
        return data

    async def renew_subscription(self) -> dict:
        """Renew the current subscription to extend its lifetime."""
        if not self._subscription_id:
            logger.warning("No active subscription to renew, creating new one")
            return await self.create_subscription()

        headers = await graph_auth.get_headers()

        expiration = datetime.now(timezone.utc) + timedelta(minutes=SUBSCRIPTION_LIFETIME_MINUTES)

        payload = {
            "expirationDateTime": expiration.isoformat(),
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.patch(
                f"{GRAPH_BASE_URL}/subscriptions/{self._subscription_id}",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        self._expires_at = datetime.fromisoformat(
            data["expirationDateTime"].replace("Z", "+00:00")
        )

        logger.info(
            "Subscription renewed: %s, new expiry %s",
            self._subscription_id,
            self._expires_at.isoformat(),
        )
        return data

    async def delete_subscription(self) -> bool:
        """Delete the current subscription."""
        if not self._subscription_id:
            return True

        headers = await graph_auth.get_headers()

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(
                f"{GRAPH_BASE_URL}/subscriptions/{self._subscription_id}",
                headers=headers,
            )

        if response.status_code == 204:
            logger.info("Subscription deleted: %s", self._subscription_id)
            self._subscription_id = None
            self._expires_at = None
            return True

        logger.error("Failed to delete subscription: %s", response.status_code)
        return False

    async def ensure_active(self) -> dict:
        """Create or renew subscription to ensure it's active."""
        if self.is_active:
            # Check if close to expiry (within 10 minutes)
            time_left = (self._expires_at - datetime.now(timezone.utc)).total_seconds()
            if time_left > 600:
                return {"status": "active", "id": self._subscription_id}
            return await self.renew_subscription()
        return await self.create_subscription()

    def start_auto_renewal(self) -> None:
        """Start the background auto-renewal loop."""
        if self._renewal_task and not self._renewal_task.done():
            return  # already running
        self._renewal_task = asyncio.create_task(self._auto_renewal_loop())
        logger.info("Subscription auto-renewal started")

    def stop_auto_renewal(self) -> None:
        """Stop the background auto-renewal loop."""
        if self._renewal_task and not self._renewal_task.done():
            self._renewal_task.cancel()
            logger.info("Subscription auto-renewal stopped")

    async def _auto_renewal_loop(self) -> None:
        """Background loop that renews the subscription every 50 minutes."""
        RENEWAL_INTERVAL = 50 * 60  # 50 minutes in seconds

        while True:
            try:
                await asyncio.sleep(RENEWAL_INTERVAL)
                logger.info("Auto-renewal: renewing subscription...")
                await self.ensure_active()
                logger.info("Auto-renewal: subscription renewed successfully")
            except asyncio.CancelledError:
                logger.info("Auto-renewal loop cancelled")
                break
            except Exception as e:
                logger.error("Auto-renewal failed: %s — will retry in 5 minutes", e)
                try:
                    await asyncio.sleep(300)  # wait 5 min before retry
                except asyncio.CancelledError:
                    break


# Module-level singleton
subscription_manager = SubscriptionManager()
