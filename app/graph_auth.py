"""
Azure AD authentication — client credentials flow + delegated user flow.

Client credentials: used for reading Excel, subscriptions, etc.
Delegated (user) flow: used for sending Teams messages (avoids 403 on group chats).
"""

import json
import os
import time
import logging
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# File to persist the delegated refresh token across restarts
_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "delegated_token.json")


class GraphAuth:
    """
    Manages Microsoft Graph API authentication.

    - Client credentials flow (app-only) for Excel, subscriptions, etc.
    - Delegated flow (user login) for sending Teams chat messages.
    """

    TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    AUTH_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
    SCOPE = "https://graph.microsoft.com/.default"
    DELEGATED_SCOPES = "Chat.ReadWrite ChatMessage.Send offline_access"
    REFRESH_BUFFER_SECONDS = 300  # refresh 5 min before expiry

    def __init__(self):
        # App-only token
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Delegated (user) token
        self._user_token: str | None = None
        self._user_token_expires_at: float = 0.0
        self._refresh_token: str | None = None
        # Load saved refresh token
        self._load_refresh_token()

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

    @property
    def auth_url(self) -> str:
        return self.AUTH_URL_TEMPLATE.format(tenant_id=self.tenant_id)

    # ── App-only (client credentials) ──

    async def get_token(self) -> str:
        if self._token and time.time() < (self._token_expires_at - self.REFRESH_BUFFER_SECONDS):
            return self._token
        logger.info("Fetching new Graph API access token (app-only)")
        await self._fetch_token()
        return self._token

    async def _fetch_token(self) -> None:
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
        token = await self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── Delegated (user) flow ──

    def get_login_url(self, redirect_uri: str) -> str:
        """Build the URL the user visits to sign in."""
        params = (
            f"client_id={self.client_id}"
            f"&response_type=code"
            f"&redirect_uri={redirect_uri}"
            f"&response_mode=query"
            f"&scope={self.DELEGATED_SCOPES.replace(' ', '%20')}"
            f"&state=agile-copilot"
        )
        return f"{self.auth_url}?{params}"

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        """Exchange authorization code for user access + refresh tokens."""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "scope": self.DELEGATED_SCOPES,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(self.token_url, data=data)
            resp.raise_for_status()
            body = resp.json()

        self._user_token = body["access_token"]
        self._refresh_token = body.get("refresh_token")
        expires_in = body.get("expires_in", 3600)
        self._user_token_expires_at = time.time() + expires_in
        self._save_refresh_token()
        logger.info("Delegated token acquired for user (expires in %ds)", expires_in)

    async def get_user_token(self) -> str | None:
        """Return a valid delegated user token, refreshing if needed."""
        if self._user_token and time.time() < (self._user_token_expires_at - self.REFRESH_BUFFER_SECONDS):
            return self._user_token

        if not self._refresh_token:
            return None

        # Refresh the token
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
            "scope": self.DELEGATED_SCOPES,
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(self.token_url, data=data)
                resp.raise_for_status()
                body = resp.json()

            self._user_token = body["access_token"]
            self._refresh_token = body.get("refresh_token", self._refresh_token)
            expires_in = body.get("expires_in", 3600)
            self._user_token_expires_at = time.time() + expires_in
            self._save_refresh_token()
            logger.info("Delegated token refreshed (expires in %ds)", expires_in)
            return self._user_token
        except Exception as e:
            logger.error("Failed to refresh delegated token: %s", e)
            self._user_token = None
            return None

    async def get_user_headers(self) -> dict | None:
        """Return Authorization headers using delegated user token."""
        token = await self.get_user_token()
        if not token:
            return None
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @property
    def has_user_token(self) -> bool:
        return self._refresh_token is not None

    def _save_refresh_token(self):
        try:
            with open(_TOKEN_FILE, "w") as f:
                json.dump({"refresh_token": self._refresh_token}, f)
        except Exception as e:
            logger.warning("Could not save refresh token: %s", e)

    def _load_refresh_token(self):
        try:
            with open(_TOKEN_FILE, "r") as f:
                data = json.load(f)
                self._refresh_token = data.get("refresh_token")
                if self._refresh_token:
                    logger.info("Loaded saved delegated refresh token")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Could not load refresh token: %s", e)


# Module-level singleton
graph_auth = GraphAuth()
