import asyncio
from datetime import date, datetime

import pytest

import app.scheduler as scheduler_module
import social_media_reminder as reminder
from app.config import settings


@pytest.mark.asyncio
async def test_preview_builds_message_without_sending(monkeypatch):
    sheets = [{"name": "Calendar", "values": []}]
    items = [
        {
            "platform": "Instagram",
            "brand": "WOGOM",
            "placement": "Post",
            "type": "Carousel",
            "topic": "Launch",
            "link": "",
            "assigned_to": "Krishna",
            "is_agency": False,
            "is_festival": False,
        }
    ]

    async def fake_read(_client):
        return sheets

    async def fake_extract(_client, received_sheets, target):
        assert received_sheets == sheets
        assert target == date(2026, 8, 5)
        return items

    async def unexpected_send(*_args):
        pytest.fail("preview must not send a Teams message")

    monkeypatch.setattr(reminder, "read_workbook", fake_read)
    monkeypatch.setattr(reminder, "extract_plan_with_groq", fake_extract)
    monkeypatch.setattr(reminder, "send_to_teams", unexpected_send)

    result = await reminder.run_once(target=date(2026, 8, 5), send=False)

    assert result["count"] == 1
    assert "Launch" in result["html"]


def test_required_config_uses_application_settings(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "wrong-direct-environment-value")
    monkeypatch.setattr(settings, "AZURE_TENANT_ID", "configured-by-app-settings")
    assert reminder.required_env("AZURE_TENANT_ID") == "configured-by-app-settings"


def test_required_config_reports_missing_value(monkeypatch):
    monkeypatch.setattr(settings, "AZURE_TENANT_ID", "")
    with pytest.raises(RuntimeError, match="AZURE_TENANT_ID is not configured"):
        reminder.required_env("AZURE_TENANT_ID")


@pytest.mark.asyncio
async def test_teams_webhook_delivery_posts_expected_payload(monkeypatch):
    monkeypatch.setattr(
        settings, "SOCIAL_TEAMS_WEBHOOK_URL", "https://teams.example.test/webhook"
    )
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    await reminder.send_to_teams(Client(), "<b>Social reminder</b><br>Tomorrow")

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://teams.example.test/webhook"
    assert kwargs["json"]["type"] == "message"
    assert "**Social reminder**" in kwargs["json"]["attachments"][0]["content"]["body"][0]["text"]


@pytest.mark.asyncio
async def test_scheduler_retries_social_reminder_after_failure(monkeypatch):
    scheduler = scheduler_module.Scheduler()
    scheduler._running = True
    attempts = 0
    sleeps = 0

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 4, 12, 5, tzinfo=tz)

    async def social_callback():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")

    async def noop():
        return None

    monkeypatch.setattr(scheduler_module, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_module.settings, "SOCIAL_REMINDER_TIME_IST", "12:00")
    # Preserve the real zero-delay yield used inside the patched sleep.
    real_sleep = asyncio.tasks.sleep
    async def bounded_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            scheduler._running = False
        await real_sleep(0)
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", bounded_sleep)

    await scheduler._loop(noop, noop, noop, noop, social_callback)

    assert attempts == 2


@pytest.mark.asyncio
async def test_fastapi_social_delivery_uses_configured_chat(monkeypatch):
    import app.main as main

    monkeypatch.setattr(settings, "SOCIAL_TEAMS_CHAT_ID", "social-chat-id")
    result = {
        "date": "2026-08-05",
        "count": 1,
        "html": "<b>Social reminder</b>",
    }
    sent = []

    async def fake_build():
        return result

    async def fake_send(content, chat_id=None):
        sent.append((content, chat_id))

    monkeypatch.setattr(main, "_build_social_media_reminder", fake_build)
    monkeypatch.setattr(main, "_send_teams_message", fake_send)

    assert await main._send_social_media_reminder() == result
    assert sent == [(result["html"], "social-chat-id")]
