"""
Azure AD authentication — client credentials flow with token caching.
"""

import time
import logging
import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class GraphAuth:
    """
    Manages Microsoft Graph API authentication using the OAuth 2.0
    client credentials flow (app-only, no user login).

    Caches the access token and auto-refreshes before expiry.
    """

    TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    SCOPE = "https://graph.microsoft.com/.default"
    REFRESH_BUFFER_SECONDS = 300  # refresh 5 min before expiry

    def __init__(self):
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def tenant_id(self) -> str:
        return settings.AZURE_TENANT_ID

    @property
    def client_id(self) -> str:
        return settings.AZURE_CLIENT_ID

    @property
    def client_secret(self) -> str:
        return settings.AZURE_CLIENT_SECRET

    @property
    def token_url(self) -> str:
        return self.TOKEN_URL_TEMPLATE.format(tenant_id=self.tenant_id)

    async def get_token(self) -> str:
        """
        Return a valid access token. Fetches a new one if the cached token
        is expired or about to expire.
        """
        if self._token and time.time() < (self._token_expires_at - self.REFRESH_BUFFER_SECONDS):
            return self._token

        logger.info("Fetching new Graph API access token")
        await self._fetch_token()
        return self._token

    async def _fetch_token(self) -> None:
        """Request a new access token from Azure AD."""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.SCOPE,
            "grant_type": "client_credentials",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(self.token_url, data=data)
            response.raise_for_status()
            body = response.json()

        self._token = body["access_token"]
        expires_in = body.get("expires_in", 3600)
        self._token_expires_at = time.time() + expires_in
        logger.info("Token acquired, expires in %d seconds", expires_in)

    async def get_headers(self) -> dict:
        """Return Authorization headers for Graph API calls."""
        token = await self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }


# Module-level singleton
graph_auth = GraphAuth()
