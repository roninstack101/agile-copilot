"""
Agile Copilot — FastAPI application.

Endpoints:
  POST /api/eod-webhook        — receive EOD payload (manual or from Power Automate)
  POST /api/graph-webhook       — receive Graph API subscription notifications
  GET  /api/graph-webhook       — handle Graph API validation handshake
  POST /api/subscribe           — create/renew Graph API subscription
  POST /api/notify-wip          — send WIP task summary to Teams group chat
  POST /api/eod-reminder        — send EOD reminder to Teams group chat
  POST /api/morning-summary     — send AI-prioritized morning summary to Teams
  GET  /api/login               — start delegated auth (sign in to send Teams messages)
  GET  /api/auth-callback       — OAuth callback to capture auth code
  GET  /health                  — health check
"""

import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel

from app.config import settings, get_sprint_end_date
from app.teams_capture import extract_metadata, is_eod_message, validate_eod
from app.ai_parser import parse_eod
from app.validator import validate_all
from app.excel_writer import (
    write_tasks, resolve_sheet_name,
    read_sheet_context, update_backlog_row,
    list_all_sheets, get_existing_rows,
)
from app.task_router import route_tasks
from app.subscription_manager import subscription_manager
from app.scheduler import scheduler

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# App lifecycle
# ──────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    logger.info("Agile Copilot starting up")

    # Start auto-renewal if subscription is already active
    # (subscription is created manually via POST /api/subscribe)
    subscription_manager.start_auto_renewal()
    logger.info("Subscription auto-renewal started")

    # Start daily scheduler (9:30 todo, 10:15 agile reminder, 11:30 progress, 6PM EOD)
    scheduler.start(
        eod_callback=_send_eod_reminder,
        morning_callback=_send_agile_reminder,
        progress_callback=_send_progress_report,
        todo_callback=_send_morning_summary,
    )

    yield

    # Cleanup
    scheduler.stop()
    subscription_manager.stop_auto_renewal()
    if subscription_manager.is_active:
        try:
            await subscription_manager.delete_subscription()
        except Exception as e:
            logger.warning("Failed to delete subscription on shutdown: %s", e)

    logger.info("Agile Copilot shutting down")


# Simple dedup cache to prevent processing the same Graph notification twice
_processed_messages: set[str] = set()
_MAX_CACHE_SIZE = 200

app = FastAPI(
    title="Agile Copilot",
    description="Automated AI agent that parses MS Teams EOD updates and fills the agile Excel sheet.",
    version="1.0.0",
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# Delegated auth — one-time user login for Teams messaging
# ──────────────────────────────────────────────


@app.get("/api/login")
async def login(request: Request):
    """Redirect user to Microsoft login to grant Teams messaging permission."""
    from app.graph_auth import graph_auth

    redirect_uri = settings.REDIRECT_URI or f"{str(request.base_url).rstrip('/')}/api/auth-callback"
    login_url = graph_auth.get_login_url(redirect_uri)
    return RedirectResponse(login_url)


@app.get("/api/auth-callback")
async def auth_callback(request: Request, code: str = "", error: str = ""):
    """OAuth callback — exchange code for delegated token."""
    from app.graph_auth import graph_auth

    if error:
        return HTMLResponse(f"<h2>Login failed</h2><p>{error}</p>", status_code=400)

    if not code:
        return HTMLResponse("<h2>No authorization code received</h2>", status_code=400)

    redirect_uri = settings.REDIRECT_URI or f"{str(request.base_url).rstrip('/')}/api/auth-callback"

    try:
        await graph_auth.exchange_code(code, redirect_uri)
        return HTMLResponse(
            "<h2>Login successful!</h2>"
            "<p>Agile Copilot can now send messages to your Teams chats.</p>"
            "<p>You can close this tab.</p>"
        )
    except Exception as e:
        logger.error("Auth callback failed: %s", e)
        return HTMLResponse(f"<h2>Login failed</h2><p>{e}</p>", status_code=500)


# ──────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────


class EODPayload(BaseModel):
    """Direct EOD webhook payload (e.g. from Power Automate or manual POST)."""
    sender: str = ""
    message: str = ""
    timestamp: str = ""


class PipelineResult(BaseModel):
    """Result of the EOD processing pipeline."""
    status: str
    member: str
    tasks_parsed: int
    tasks_appended: int
    tasks_updated: int
    errors: list[str] = []


# ──────────────────────────────────────────────
# EOD processing pipeline
# ──────────────────────────────────────────────


async def _process_eod(sender: str, clean_message: str, timestamp: str) -> PipelineResult:
    """
    Full pipeline:
      1. Fetch backlog context from sheet
      2. Parse EOD with AI (Gemini → Groq → local)
      3. Validate parsed tasks (dedup, defaults, schema)
      4. Write to Excel sheet
    """
    logger.info("Processing EOD from '%s'", sender)

    # Resolve which worksheet belongs to this member
    try:
        sheet_name = await resolve_sheet_name(sender)
    except Exception as e:
        logger.error("Failed to resolve sheet for '%s': %s", sender, e)
        return PipelineResult(
            status="skipped",
            member=sender,
            tasks_parsed=0,
            tasks_appended=0,
            tasks_updated=0,
            errors=[f"No sheet found for '{sender}'"],
        )

    # Skip if no matching sheet was found
    if not sheet_name:
        logger.info("No sheet available for '%s' — skipping", sender)
        return PipelineResult(
            status="skipped",
            member=sender,
            tasks_parsed=0,
            tasks_appended=0,
            tasks_updated=0,
            errors=[f"No sheet found for '{sender}'"],
        )

    logger.info("Using worksheet '%s' for member '%s'", sheet_name, sender)

    # Build context for the parser — single sheet read
    sprint_end = get_sprint_end_date()
    today = date.today().isoformat()

    try:
        sheet_ctx = await read_sheet_context(sheet_name=sheet_name)
    except Exception as e:
        logger.warning("Failed to read sheet context: %s — continuing with empty context", e)
        sheet_ctx = {
            "backlog_items": [], "existing_rows": [],
            "backlog_list": [], "header_idx": 0,
        }

    backlog = sheet_ctx["backlog_list"]
    existing_rows = sheet_ctx["existing_rows"]

    context = {
        "member_name": sender,
        "today_date": today,
        "sprint_end_date": sprint_end,
        "backlog_list": backlog,
        "existing_rows": existing_rows,
    }

    logger.info(
        "Sheet context: %d existing rows, %d backlog items",
        len(existing_rows),
        len(backlog),
    )

    # Step 2: Parse EOD
    logger.info("Clean message for parsing:\n%s", clean_message)
    try:
        tasks = await parse_eod(clean_message, context)
    except Exception as e:
        logger.error("All parsers failed: %s", e)
        return PipelineResult(
            status="error",
            member=sender,
            tasks_parsed=0,
            tasks_appended=0,
            tasks_updated=0,
            errors=[f"Parsing failed: {e}"],
        )

    if not tasks:
        return PipelineResult(
            status="empty",
            member=sender,
            tasks_parsed=0,
            tasks_appended=0,
            tasks_updated=0,
        )

    # Log parsed tasks for debugging
    for i, t in enumerate(tasks):
        logger.info(
            "Parsed task %d: sprint_backlog='%s', brand='%s', activity='%s'",
            i, t.get("sprint_backlog", ""), t.get("brand", ""), t.get("activity_type", ""),
        )

    # Step 3: Validate
    new_tasks, update_tasks = validate_all(tasks, existing_rows, backlog, sprint_end)

    logger.info("After validation: %d new tasks, %d updates", len(new_tasks), len(update_tasks))

    # Step 3.5: Route tasks — backlog promotion (in-place updates)
    routed_tasks, inplace_updates = route_tasks(
        new_tasks, sheet_ctx["backlog_items"],
    )

    # Step 3.6: Write backlog in-place updates (task written on same row as backlog item)
    for task in inplace_updates:
        try:
            await update_backlog_row(
                sheet_name, task, task["_backlog_row_idx"], task["_backlog_col_idx"]
            )
            logger.info(
                "Backlog in-place: wrote '%s' on row %d",
                task.get("sprint_backlog", ""), task["_backlog_row_idx"] + 1,
            )
        except Exception as e:
            logger.warning(
                "Failed to update backlog row %d: %s", task["_backlog_row_idx"], e
            )

    # Step 4: Write to member's Excel sheet
    try:
        write_result = await write_tasks(routed_tasks, update_tasks, sheet_name=sheet_name)
    except Exception as e:
        logger.error("Failed to write to Excel: %s", e)
        return PipelineResult(
            status="error",
            member=sender,
            tasks_parsed=len(tasks),
            tasks_appended=0,
            tasks_updated=0,
            errors=[f"Excel write failed: {e}"],
        )

    return PipelineResult(
        status="success",
        member=sender,
        tasks_parsed=len(tasks),
        tasks_appended=write_result.get("appended", 0),
        tasks_updated=write_result.get("updated", 0) + len(inplace_updates),
        errors=write_result.get("errors", []),
    )


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "agile-copilot",
        "subscription_active": subscription_manager.is_active,
    }


@app.post("/api/eod-webhook", response_model=PipelineResult)
async def eod_webhook(payload: EODPayload):
    """
    Receive a direct EOD payload and process it through the pipeline.
    Used with Power Automate or manual testing.
    """
    metadata = extract_metadata(payload.model_dump())
    sender = metadata["sender"]
    clean_message = metadata["clean_message"]
    timestamp = metadata["timestamp"]

    if not validate_eod(clean_message):
        raise HTTPException(
            status_code=400,
            detail="Message does not appear to be a valid EOD (no tasks found).",
        )

    return await _process_eod(sender, clean_message, timestamp)


@app.get("/api/graph-webhook")
async def graph_webhook_validation(request: Request):
    """
    Handle the Graph API subscription validation handshake.
    Graph sends a GET with ?validationToken=<token> and expects it echoed back.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        logger.info("Graph API validation handshake received")
        return Response(content=validation_token, media_type="text/plain")
    return Response(content="OK", media_type="text/plain")


@app.post("/api/graph-webhook")
async def graph_webhook_notification(request: Request):
    """
    Receive notification from Graph API when a new message is posted
    in the subscribed Teams channel.

    Graph sends a batch of notifications; each contains the resource path
    to the new message. We need to fetch the message content via Graph API
    and then process it.
    """
    # Graph may send validation as POST with ?validationToken query param
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        logger.info("Graph API validation handshake received (POST)")
        return Response(content=validation_token, media_type="text/plain")

    body = await request.json()

    # Validate the client state to confirm it's our subscription
    notifications = body.get("value", [])

    for notification in notifications:
        client_state = notification.get("clientState", "")
        if client_state != "agile-copilot-secret":
            logger.warning("Invalid clientState in notification, skipping")
            continue

        resource = notification.get("resource", "")
        logger.info("Graph notification for resource: %s", resource)

        # Dedup: skip if we've already processed this resource (message ID)
        if resource in _processed_messages:
            logger.info("Duplicate notification for '%s' — skipping", resource)
            continue
        if len(_processed_messages) > _MAX_CACHE_SIZE:
            _processed_messages.clear()
        _processed_messages.add(resource)

        # Fetch the message content from Graph API
        try:
            message_data = await _fetch_message(resource)
        except Exception as e:
            logger.error("Failed to fetch message from Graph: %s", e)
            continue

        # Skip messages sent by this app (bot) or via delegated token (self-loop prevention)
        from_info = message_data.get("from", {})
        app_info = from_info.get("application")
        user_info = from_info.get("user")
        if app_info and app_info.get("id") == settings.AZURE_CLIENT_ID:
            logger.info("Skipping message sent by this app (application ID match)")
            continue
        # Skip messages from the delegated user (Yash) that contain bot signatures
        msg_body = message_data.get("body", {}).get("content", "")
        if any(tag in msg_body for tag in ["Good Morning! Daily Focus", "EOD Reminder", "WIP Task Summary"]):
            logger.info("Skipping bot-generated message (signature detected)")
            continue

        # Extract metadata and check if it's an EOD
        metadata = extract_metadata(message_data)
        logger.info("Raw HTML from Teams:\n%s", metadata["raw_message"][:500])
        logger.info("Clean text after stripping:\n%s", metadata["clean_message"])
        if not is_eod_message(metadata["clean_message"]):
            logger.info("Message from '%s' is not an EOD — skipping", metadata["sender"])
            continue

        if not validate_eod(metadata["clean_message"]):
            logger.info("Message from '%s' has no valid tasks — skipping", metadata["sender"])
            continue

        # Process the EOD
        result = await _process_eod(
            metadata["sender"],
            metadata["clean_message"],
            metadata["timestamp"],
        )
        logger.info(
            "EOD processed for '%s': %d parsed, %d appended, %d updated",
            result.member,
            result.tasks_parsed,
            result.tasks_appended,
            result.tasks_updated,
        )

    return Response(status_code=202)


async def _fetch_message(resource: str) -> dict:
    """Fetch a Teams message by its Graph API resource path."""
    import httpx
    from app.graph_auth import graph_auth
    from app.config import GRAPH_BASE_URL

    headers = await graph_auth.get_headers()
    url = f"{GRAPH_BASE_URL}/{resource.lstrip('/')}"

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


@app.post("/api/subscribe")
async def create_subscription():
    """
    Manually create or renew the Graph API subscription for the Teams channel.
    """
    try:
        result = await subscription_manager.ensure_active()
        return {"status": "ok", "subscription": result}
    except Exception as e:
        logger.error("Subscription failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Subscription failed: {e}")


# ──────────────────────────────────────────────
# WIP notification
# ──────────────────────────────────────────────


async def _send_teams_message(content: str) -> None:
    """Send a message to the Teams group chat.

    Uses delegated (user) token first — this works for group chats.
    Falls back to app-only token if delegated is not available.
    """
    import httpx
    from app.graph_auth import graph_auth
    from app.config import GRAPH_BASE_URL

    if not settings.CHAT_ID:
        raise ValueError("CHAT_ID not configured")

    url = f"{GRAPH_BASE_URL}/chats/{settings.CHAT_ID}/messages"
    payload = {"body": {"contentType": "html", "content": content}}

    # Try delegated token first (works for group chats without bot installation)
    user_headers = await graph_auth.get_user_headers()
    if user_headers:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=user_headers, json=payload)
            resp.raise_for_status()
            logger.info("Teams message sent via delegated token")
            return

    # Fall back to app-only token
    logger.warning("No delegated token — falling back to app-only token (may 403 on group chats)")
    headers = await graph_auth.get_headers()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()


@app.post("/api/test-message")
async def test_message():
    """Send a test message to the Teams group chat."""
    await _send_teams_message("<b>Agile Copilot is live!</b> This is a test message from your AWS server.")
    return {"status": "ok", "message": "Test message sent"}


@app.post("/api/notify-wip")
async def notify_wip(send: bool = True):
    """
    Read all member sheets, collect WIP tasks, and send a summary to Teams.
    Use ?send=false to preview without sending.
    """
    members = await list_all_sheets()
    if not members:
        raise HTTPException(status_code=500, detail="Could not list worksheets")

    all_summaries = []
    member_data = []

    for member in members:
        try:
            rows = await get_existing_rows(sheet_name=member)
            wip_tasks = [r for r in rows if r.get("stage") == "WIP" and r.get("sprint_backlog")]
            if not wip_tasks:
                continue

            lines = []
            task_list = []
            for t in wip_tasks:
                brand = t.get("brand", "")
                activity = t.get("activity_type", "")
                priority = t.get("priority", "Medium")
                name = t.get("sprint_backlog", "")
                tag = f" ({brand} - {activity})" if brand and activity else f" ({brand or activity})" if brand or activity else ""
                lines.append(f"&bull; {name}{tag} — {priority}")
                task_list.append({"name": name, "brand": brand, "activity_type": activity, "priority": priority})

            summary = (
                f"<b>{member}</b> — {len(wip_tasks)} task(s) in progress<br>"
                + "<br>".join(lines)
            )
            all_summaries.append(summary)
            member_data.append({"member": member, "wip_count": len(wip_tasks), "tasks": task_list})
        except Exception as e:
            logger.warning("Failed to read WIP for '%s': %s", member, e)

    if not all_summaries:
        return {"status": "ok", "message": "No WIP tasks found for any member", "data": []}

    html = (
        "<b>Pending WIP Tasks</b><br><br>"
        + "<br><br>".join(all_summaries)
    )

    if not send:
        return {"status": "preview", "members": len(member_data), "data": member_data, "html": html}

    try:
        await _send_teams_message(html)
        logger.info("WIP notification sent for %d members", len(all_summaries))
        return {"status": "ok", "members_notified": len(all_summaries), "data": member_data}
    except Exception as e:
        logger.error("Failed to send Teams message: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send Teams message: {e}")


# ──────────────────────────────────────────────
# EOD Reminder (6 PM)
# ──────────────────────────────────────────────


async def _send_eod_reminder():
    """Send EOD reminder to the Teams group chat."""
    members = await list_all_sheets()
    member_list = ", ".join(members) if members else "Team"

    html = (
        "<b>EOD Reminder</b><br><br>"
        f"Hey {member_list}! It's 6 PM — time to submit your End-of-Day update.<br><br>"
        "Please share what you worked on today in the format:<br>"
        "&bull; Task 1 — status<br>"
        "&bull; Task 2 — status<br><br>"
        "<i>Tip: mention 'done' or 'completed' for finished tasks, "
        "'review pending' for tasks sent for approval.</i>"
    )

    await _send_teams_message(html)
    logger.info("EOD reminder sent")


@app.post("/api/eod-reminder")
async def eod_reminder():
    """Manually trigger the 6 PM EOD reminder."""
    try:
        await _send_eod_reminder()
        return {"status": "ok", "message": "EOD reminder sent"}
    except Exception as e:
        logger.error("EOD reminder failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send EOD reminder: {e}")


# ──────────────────────────────────────────────
# Morning Summary (10 AM) — AI-prioritized
# ──────────────────────────────────────────────


async def _ai_prioritize_tasks(member: str, wip_tasks: list[dict]) -> list[dict]:
    """Use Gemini to pick the top 5 tasks a member should focus on today."""
    import json
    import httpx

    if not settings.GEMINI_API_KEY or len(wip_tasks) <= 5:
        return wip_tasks[:5]

    task_lines = []
    for t in wip_tasks:
        brand = t.get("brand", "")
        activity = t.get("activity_type", "")
        priority = t.get("priority", "")
        name = t.get("sprint_backlog", "")
        sp = t.get("expected_story_points", 0)
        task_lines.append(f"- {name} (brand: {brand}, activity: {activity}, priority: {priority}, story points: {sp})")

    prompt = (
        f"You are an agile project manager. {member} has these WIP tasks:\n\n"
        + "\n".join(task_lines)
        + "\n\nPick the TOP 5 tasks they should focus on today, considering:\n"
        "- High priority tasks first\n"
        "- Tasks with higher story points (larger effort = start early)\n"
        "- Deadlines and dependencies\n"
        "- Balance across brands/projects\n\n"
        "Return ONLY a JSON array of the task names (exact strings), ordered by priority. "
        "Example: [\"Task A\", \"Task B\", \"Task C\", \"Task D\", \"Task E\"]"
    )

    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={settings.GEMINI_API_KEY}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
            },
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "[]")
        top_names = json.loads(text)

        if not isinstance(top_names, list):
            return wip_tasks[:5]

        # Reorder wip_tasks by AI's priority
        name_to_task = {t["sprint_backlog"]: t for t in wip_tasks}
        ordered = []
        for name in top_names[:5]:
            if name in name_to_task:
                ordered.append(name_to_task[name])
        return ordered if ordered else wip_tasks[:5]

    except Exception as e:
        logger.warning("AI prioritization failed for %s: %s, using default order", member, e)
        return wip_tasks[:5]


async def _send_agile_reminder():
    """Send a reminder to update the agile sheet."""
    members = await list_all_sheets()
    member_list = ", ".join(members) if members else "Team"
    html = (
        f"<b>Agile Update Reminder</b><br><br>"
        f"Hey {member_list}! Please update your agile sheet with today's tasks and progress.<br><br>"
        "<i>Make sure to mark completed tasks as Closed and update story points.</i>"
    )
    await _send_teams_message(html)
    logger.info("Agile update reminder sent")


async def _send_morning_summary():
    """Build and send the AI-prioritized morning WIP summary."""
    members = await list_all_sheets()
    if not members:
        return

    all_summaries = []

    for member in members:
        try:
            rows = await get_existing_rows(sheet_name=member)
            wip_tasks = [r for r in rows if r.get("stage") == "WIP" and r.get("sprint_backlog")]
            if not wip_tasks:
                continue

            # AI picks top 5
            top_tasks = await _ai_prioritize_tasks(member, wip_tasks)

            lines = []
            for i, t in enumerate(top_tasks, 1):
                brand = t.get("brand", "")
                activity = t.get("activity_type", "")
                name = t.get("sprint_backlog", "")
                tag = f" ({brand} - {activity})" if brand and activity else f" ({brand or activity})" if brand or activity else ""
                lines.append(f"{i}. {name}{tag}")

            remaining = len(wip_tasks) - len(top_tasks)
            task_word = "task" if len(top_tasks) == 1 else "tasks"
            summary = f"<b>{member}</b> — Top {len(top_tasks)} focus {task_word}:<br>" + "<br>".join(lines)
            if remaining > 0:
                summary += f"<br><i>+{remaining} more WIP tasks</i>"

            all_summaries.append(summary)
        except Exception as e:
            logger.warning("Failed morning summary for '%s': %s", member, e)

    if not all_summaries:
        return

    today = date.today().strftime("%A, %B %d")
    html = (
        f"<b>Good Morning! Daily Focus — {today}</b><br><br>"
        + "<br><br>".join(all_summaries)
        + "<br><br><i>Prioritized by AI based on effort, priority, and project balance.</i>"
    )

    await _send_teams_message(html)
    logger.info("Morning summary sent for %d members", len(all_summaries))


@app.post("/api/agile-reminder")
async def agile_reminder():
    """Manually trigger the 10:15 AM agile update reminder."""
    try:
        await _send_agile_reminder()
        return {"status": "ok", "message": "Agile update reminder sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed: {e}")


@app.post("/api/morning-summary")
async def morning_summary(send: bool = True):
    """Manually trigger the 9:30 AM todo summary. Use ?send=false to preview."""
    members = await list_all_sheets()
    if not members:
        raise HTTPException(status_code=500, detail="Could not list worksheets")

    member_data = []

    for member in members:
        try:
            rows = await get_existing_rows(sheet_name=member)
            wip_tasks = [r for r in rows if r.get("stage") == "WIP" and r.get("sprint_backlog")]
            if not wip_tasks:
                continue

            top_tasks = await _ai_prioritize_tasks(member, wip_tasks)
            task_list = [
                {"name": t.get("sprint_backlog", ""), "brand": t.get("brand", ""),
                 "activity_type": t.get("activity_type", "")}
                for t in top_tasks
            ]
            member_data.append({
                "member": member,
                "total_wip": len(wip_tasks),
                "top_5": task_list,
            })
        except Exception as e:
            logger.warning("Failed morning summary for '%s': %s", member, e)

    if not member_data:
        return {"status": "ok", "message": "No WIP tasks found", "data": []}

    if not send:
        return {"status": "preview", "members": len(member_data), "data": member_data}

    try:
        await _send_morning_summary()
        return {"status": "ok", "members_notified": len(member_data), "data": member_data}
    except Exception as e:
        logger.error("Morning summary failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send morning summary: {e}")


# ──────────────────────────────────────────────
# Progress Report
# ──────────────────────────────────────────────


def _progress_bar(actual: int, expected: int, width: int = 10) -> str:
    """Return a simple text progress bar."""
    if expected <= 0:
        return "░" * width
    pct = min(actual / expected, 1.0)
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


async def _send_progress_report():
    """Read all member sheets and send actual vs expected story point progress."""
    members = await list_all_sheets()
    if not members:
        return

    lines = []
    total_actual = 0
    total_expected = 0

    for member in members:
        try:
            rows = await get_existing_rows(sheet_name=member)
            # Exclude the total/summary row if present (sprint_backlog contains "total")
            task_rows = [r for r in rows if "total" not in r.get("sprint_backlog", "").lower()]
            exp = sum(r.get("expected_story_points", 0) for r in task_rows)
            act = sum(r.get("actual_story_points", 0) for r in task_rows)
            closed = sum(1 for r in task_rows if r.get("stage") == "Closed")
            total_tasks = len(task_rows)

            if exp == 0 and total_tasks == 0:
                continue

            pct = round((act / exp) * 100) if exp > 0 else 0
            bar = _progress_bar(act, exp)
            lines.append(
                f"<b>{member}</b> — {act}/{exp} SP ({pct}%) {bar} &nbsp;|&nbsp; "
                f"{closed}/{total_tasks} tasks closed"
            )
            total_actual += act
            total_expected += exp
        except Exception as e:
            logger.warning("Failed progress for '%s': %s", member, e)

    if not lines:
        return

    today = date.today().strftime("%A, %B %d")
    team_pct = round((total_actual / total_expected) * 100) if total_expected > 0 else 0
    team_bar = _progress_bar(total_actual, total_expected)

    html = (
        f"<b>Sprint Progress — {today}</b><br><br>"
        + "<br>".join(lines)
        + f"<br><br><b>Team Total — {total_actual}/{total_expected} SP ({team_pct}%) {team_bar}</b>"
    )

    await _send_teams_message(html)
    logger.info("Progress report sent for %d members", len(lines))


@app.post("/api/progress-report")
async def progress_report(send: bool = True):
    """Send sprint progress report (actual vs expected story points) to Teams."""
    members = await list_all_sheets()
    if not members:
        raise HTTPException(status_code=500, detail="Could not list worksheets")

    report_data = []
    total_actual = 0
    total_expected = 0

    for member in members:
        try:
            rows = await get_existing_rows(sheet_name=member)
            task_rows = [r for r in rows if "total" not in r.get("sprint_backlog", "").lower()]
            exp = sum(r.get("expected_story_points", 0) for r in task_rows)
            act = sum(r.get("actual_story_points", 0) for r in task_rows)
            closed = sum(1 for r in task_rows if r.get("stage") == "Closed")
            report_data.append({
                "member": member,
                "actual_sp": act,
                "expected_sp": exp,
                "pct": round((act / exp) * 100) if exp > 0 else 0,
                "closed_tasks": closed,
                "total_tasks": len(task_rows),
            })
            total_actual += act
            total_expected += exp
        except Exception as e:
            logger.warning("Failed progress for '%s': %s", member, e)

    if not send:
        return {
            "status": "preview",
            "team_actual_sp": total_actual,
            "team_expected_sp": total_expected,
            "data": report_data,
        }

    try:
        await _send_progress_report()
        return {
            "status": "ok",
            "team_actual_sp": total_actual,
            "team_expected_sp": total_expected,
            "data": report_data,
        }
    except Exception as e:
        logger.error("Progress report failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send progress report: {e}")


# ──────────────────────────────────────────────
# Run with uvicorn
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
