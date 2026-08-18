"""Tomorrow's social-media plan: SharePoint Excel -> Groq -> Teams.

This file is intentionally standalone so the existing Agile Copilot flow does
not need to be changed.

Examples:
    python social_media_reminder.py --preview
    python social_media_reminder.py --date 2026-07-31 --preview
    python social_media_reminder.py --send
    python social_media_reminder.py --daemon
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


GRAPH = "https://graph.microsoft.com/v1.0"
IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "delegated_token.json"
STATE_FILE = ROOT / "social_reminder_state.json"
SOCIAL_PLATFORMS = ("Instagram", "LinkedIn", "YouTube")


def load_env() -> None:
    """Load the project's single .env file without another dependency."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value and value[:1] == value[-1:] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Populate os.environ from .env as soon as this module is imported, so
# required_env()/os.getenv() below work both when this file is run standalone
# and when app/main.py imports run_once() into the existing FastAPI/scheduler flow.
load_env()


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured in .env")
    return value


def sharing_token(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"u!{encoded}"


async def app_token(client: httpx.AsyncClient) -> str:
    tenant = required_env("AZURE_TENANT_ID")
    response = await client.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": required_env("AZURE_CLIENT_ID"),
            "client_secret": required_env("AZURE_CLIENT_SECRET"),
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


async def delegated_token(client: httpx.AsyncClient) -> str:
    """Refresh the delegated token created by the existing /api/login flow."""
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            "delegated_token.json is missing. Sign in once through the existing "
            "Agile Copilot /api/login endpoint before sending to Teams."
        )
    refresh_token = json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get(
        "refresh_token"
    )
    if not refresh_token:
        raise RuntimeError("No refresh_token found in delegated_token.json")

    tenant = required_env("AZURE_TENANT_ID")
    response = await client.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": required_env("AZURE_CLIENT_ID"),
            "client_secret": required_env("AZURE_CLIENT_SECRET"),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": "Chat.ReadWrite ChatMessage.Send offline_access",
        },
    )
    response.raise_for_status()
    body = response.json()
    if body.get("refresh_token") and body["refresh_token"] != refresh_token:
        TOKEN_FILE.write_text(
            json.dumps({"refresh_token": body["refresh_token"]}), encoding="utf-8"
        )
    return body["access_token"]


async def read_workbook(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Read every used worksheet range through the workbook's sharing URL."""
    token = await app_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    share = sharing_token(required_env("SOCIAL_EXCEL_URL"))

    item_response = await client.get(
        f"{GRAPH}/shares/{share}/driveItem?$select=id,parentReference,name",
        headers=headers,
    )
    item_response.raise_for_status()
    item = item_response.json()
    drive_id = item["parentReference"]["driveId"]
    item_id = item["id"]

    sheets_response = await client.get(
        f"{GRAPH}/drives/{drive_id}/items/{item_id}/workbook/worksheets"
        "?$select=id,name,visibility",
        headers=headers,
    )
    sheets_response.raise_for_status()

    sheets: list[dict[str, Any]] = []
    for sheet in sheets_response.json().get("value", []):
        if sheet.get("visibility", "Visible").lower() != "visible":
            continue
        sheet_id = sheet["id"]
        range_response = await client.get(
            f"{GRAPH}/drives/{drive_id}/items/{item_id}/workbook/"
            f"worksheets/{sheet_id}/usedRange(valuesOnly=true)"
            "?$select=values,text,address",
            headers=headers,
        )
        range_response.raise_for_status()
        used = range_response.json()
        sheets.append(
            {
                "name": sheet["name"],
                "address": used.get("address", ""),
                "values": used.get("text") or used.get("values") or [],
            }
        )
    return sheets


def _cell_date(value: Any) -> date | None:
    text = str(value).strip()
    for fmt in (
        "%d-%b-%y", "%d-%b-%Y", "%d %b %y", "%d %b %Y",
        "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def relevant_workbook_rows(
    sheets: list[dict[str, Any]], target: date
) -> list[dict[str, Any]]:
    """Keep sheet headers and only rows whose first cells contain target."""
    relevant: list[dict[str, Any]] = []
    for sheet in sheets:
        headers: list[tuple[int, list[Any]]] = []
        matches: list[tuple[int, list[Any]]] = []
        reached_data = False
        for number, row in enumerate(sheet.get("values", []), 1):
            row_date = next((_cell_date(cell) for cell in row[:3] if str(cell).strip()), None)
            if row_date:
                reached_data = True
                if row_date == target:
                    matches.append((number, row))
            elif not reached_data:
                headers.append((number, row))
        if matches:
            relevant.append(
                {"name": sheet["name"], "numbered_rows": headers + matches}
            )
    return relevant


def compact_workbook(sheets: list[dict[str, Any]], target: date) -> str:
    """Create a target-date-only representation suitable for Groq."""
    output: list[str] = []
    for sheet in relevant_workbook_rows(sheets, target):
        output.append(f"\nWORKSHEET: {sheet['name']}")
        for number, row in sheet["numbered_rows"]:
            cells = [str(cell).strip() for cell in row]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                output.append(f"R{number}: " + " | ".join(cells))
    return "\n".join(output)


def _json_array(text: str) -> list[dict[str, Any]]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    value = json.loads(text)
    if isinstance(value, dict):
        value = value.get("items", [])
    if not isinstance(value, list):
        raise ValueError("Groq response is not a JSON array")
    return [item for item in value if isinstance(item, dict)]


async def extract_plan_with_ai(
    client: httpx.AsyncClient, sheets: list[dict[str, Any]], target: date
) -> list[dict[str, Any]]:
    workbook = compact_workbook(sheets, target)
    if not workbook.strip():
        return []
    max_chars = int(os.getenv("SOCIAL_MAX_WORKBOOK_CHARS", "90000"))
    if len(workbook) > max_chars:
        raise RuntimeError(
            f"Workbook data is too large ({len(workbook)} characters). "
            "Set SOCIAL_MAX_WORKBOOK_CHARS higher or archive old rows."
        )

    prompt = f"""
You read social-media calendars. Extract ONLY content scheduled on
{target.isoformat()} ({target.strftime('%d %B %Y')}).

The workbook may contain three horizontal or vertical sections: Instagram,
LinkedIn, and YouTube. A brand may be Wogom, Abaj, World EMS/WEMS, WOGOM
LinkedIn, or another name. Preserve the exact topic/title and URL. Determine
the assignee only from the workbook; never invent one. "Post", "Story",
"Reel", "Short", and "Video" are content types, not brands.

Return ONLY a JSON array. Every object must use exactly these keys:
"platform", "brand", "placement", "type", "topic", "link", "assigned_to",
"is_agency", "is_festival".
Normalize platform to Instagram, LinkedIn, or YouTube. If a field is absent,
use an empty string, except "is_agency" and "is_festival", which must be true
or false. For Instagram, "placement" must be "Post" or "Story". A value in a
STORY column is always an Instagram Story, even when the cell text looks like
another date (for example "9th July"); preserve that cell as the topic. Regular
Instagram CONTENT columns are Posts. Keep the creative format in "type", such
as Video, Reel, Carousel, Article, or Static. For LinkedIn and YouTube,
"placement" is "Post" or "Upload" as appropriate. Set "is_festival" to true
when the content comes from any FESTIVAL, FESTIVALS, or FESTIVALS - Story
column, or is clearly identified as festival/holiday content. Festival content
must still have its platform, placement, brand, topic, and assignee extracted.
Apply these workbook-specific rules: ABAJ festival entries are Instagram
Stories; WEMS festival entries are Instagram Posts; STORY columns and regular
Instagram CONTENT/Post columns are separate and both must be extracted when
both contain content. WOGOM YouTube repeats its regular Instagram Post, so do
not invent a separate YouTube item from an empty status or checkbox.
Set
"is_agency" to true when the workbook says Agency, external agency, or otherwise
clearly indicates that the agency will create, handle, publish, or post that
content. Do not infer agency merely because the assignee is missing. Include
separate objects when the same brand has both a Post and a Story. Ignore rows
belonging to every other date. Do not return an item when both type and topic
are empty. FALSE, unchecked status cells, empty descriptions, and TBA by itself
are not scheduled content. Each platform occupies its own column section:
never copy Instagram content into YouTube or LinkedIn merely because those
platform headers exist elsewhere in the same row.

WORKBOOK:
{workbook}
""".strip()

    response = await client.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {required_env('DEEPSEEK_API_KEY')}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("SOCIAL_DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract calendar rows accurately. Return an object "
                        'with one key, "items", containing the requested array.'
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        },
    )
    response.raise_for_status()
    text = response.json()["choices"][0]["message"]["content"]
    items = _json_array(text)

    cleaned: list[dict[str, Any]] = []
    for item in items:
        platform = str(item.get("platform", "")).strip()
        canonical = next(
            (p for p in SOCIAL_PLATFORMS if p.lower() == platform.lower()), platform
        )
        def as_bool(value: Any) -> bool:
            return (
                value
                if isinstance(value, bool)
                else str(value).strip().lower() in {"true", "yes", "1", "agency"}
            )

        is_agency = as_bool(item.get("is_agency", False))
        is_festival = as_bool(item.get("is_festival", False))
        assigned_to = str(item.get("assigned_to", "") or "").strip()
        if re.search(r"\bagenc(?:y|ies)\b", assigned_to, flags=re.IGNORECASE):
            is_agency = True
        cleaned_item = {
            key: str(item.get(key, "") or "").strip()
            for key in (
                "brand", "placement", "type", "topic", "link", "assigned_to"
            )
        } | {
            "platform": canonical,
            "is_agency": is_agency,
            "is_festival": is_festival,
        }
        if not cleaned_item["placement"] and canonical == "Instagram":
            cleaned_item["placement"] = "Story" if cleaned_item["type"].lower() == "story" else "Post"
        if not cleaned_item["type"] and (
            not cleaned_item["topic"] or cleaned_item["topic"].lower() == "tba"
        ):
            continue
        cleaned.append(cleaned_item)
    return cleaned


def format_reminder(items: list[dict[str, Any]], target: date) -> str:
    heading = target.strftime("%A, %d %B %Y")
    sections: list[str] = []

    def brand_key(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]", "", value.lower())
        return {
            "worldemswems": "wems", "worldems": "wems", "wems": "wems",
            "abaj": "abaj", "wdv": "wdv", "brandverse": "brandverse",
            "ravisir": "ravisir", "wogom": "wogom", "niravsir": "niravsir",
        }.get(normalized, normalized)

    def select(
        brand: str,
        platform: str | None = None,
        placement: str | None = None,
        festival: bool | None = None,
    ) -> list[dict[str, Any]]:
        selected = [i for i in items if brand_key(i.get("brand", "")) == brand]
        if platform:
            selected = [i for i in selected if i.get("platform", "").lower() == platform.lower()]
        if placement:
            selected = [i for i in selected if i.get("placement", "").lower() == placement.lower()]
        if festival is not None:
            selected = [i for i in selected if bool(i.get("is_festival")) == festival]
        return selected

    def item_line(item: dict[str, Any], show_placement: bool = True) -> str:
        placement = html.escape(item.get("placement") or "")
        content_type = html.escape(item.get("type") or "")
        if placement.lower() == content_type.lower():
            content_type = ""
        parts = (
            (placement, content_type)
            if show_placement
            else (content_type or placement,)
        )
        label = " · ".join(part for part in parts if part) or "Content"
        topic = html.escape(item.get("topic") or "Topic not specified")
        link = item.get("link", "")
        topic_display = (
            f'<a href="{html.escape(link, quote=True)}">{topic}</a>'
            if link.startswith(("https://", "http://"))
            else topic
        )
        if item.get("is_agency"):
            responsibility = "<br>&nbsp;&nbsp;<b>Tomorrow it will be posted by Agency.</b>"
        else:
            assignee = html.escape(item.get("assigned_to") or "Not assigned")
            responsibility = f"<br>&nbsp;&nbsp;Assigned to: {assignee}"
        return f"&bull; <b>{label}</b>: {topic_display}" + responsibility

    def block(
        label: str,
        selected: list[dict[str, Any]],
        empty: str,
        show_placement: bool = False,
    ) -> str:
        if not selected:
            return f"<b>{label}:</b> {empty}"
        return f"<b>{label}:</b><br>" + "<br>".join(
            item_line(i, show_placement) for i in selected
        )

    def add_brand(name: str, blocks: list[str]) -> None:
        sections.append(f"<h2>{name}</h2>" + "<br><br>".join(blocks))

    add_brand("ABAJ", [
        block("Festival", select("abaj", festival=True), "No festival story tomorrow."),
        block("Instagram Story", select("abaj", "Instagram", "Story", False), "No Instagram story tomorrow."),
        block("Instagram Post", select("abaj", "Instagram", "Post", False), "No Instagram post tomorrow."),
        block("YouTube", select("abaj", "YouTube"), "No YouTube video tomorrow."),
    ])

    add_brand("WEMS", [
        block("Festival", select("wems", festival=True), "No festival post tomorrow."),
        block("Instagram Story", select("wems", "Instagram", "Story", False), "No Instagram story tomorrow."),
        block("Instagram Post", select("wems", "Instagram", "Post", False), "No Instagram post tomorrow."),
        block("LinkedIn", select("wems", "LinkedIn", festival=False), "No LinkedIn post tomorrow."),
    ])

    for key, display in (
        ("wdv", "WDV"), ("brandverse", "BRANDVERSE"), ("ravisir", "RAVI SIR")
    ):
        add_brand(display, [
            block("LinkedIn", select(key, "LinkedIn"), "No LinkedIn post tomorrow.")
        ])

    wogom_posts = select("wogom", "Instagram", "Post")
    youtube = (
        "<b>YouTube:</b> No YouTube video tomorrow."
        if not wogom_posts
        else "<b>YouTube:</b> Same as tomorrow’s Instagram post.<br>"
        + "<br>".join(item_line(i, False) for i in wogom_posts)
    )
    add_brand("WOGOM", [
        block("Instagram Story", select("wogom", "Instagram", "Story"), "No Instagram story tomorrow."),
        block("Instagram Post", wogom_posts, "No Instagram post tomorrow."),
        block("LinkedIn", select("wogom", "LinkedIn"), "No LinkedIn post tomorrow."),
        youtube,
    ])

    add_brand("NIRAV SIR", [
        block("LinkedIn", select("niravsir", "LinkedIn"), "No LinkedIn post tomorrow.")
    ])

    return (
        f"<h1>Social Media Reminder</h1><b>{html.escape(heading)}</b><br><br>"
        + "<br><br>".join(sections)
    )


async def send_to_teams(client: httpx.AsyncClient, content: str) -> None:
    webhook_url = os.getenv("SOCIAL_TEAMS_WEBHOOK_URL", "").strip()
    if webhook_url:
        # Teams Workflows incoming webhooks accept an Adaptive Card envelope.
        markdown = content
        markdown = re.sub(r"<br\s*/?>", "\n", markdown, flags=re.IGNORECASE)
        markdown = re.sub(r"<b>(.*?)</b>", r"**\1**", markdown, flags=re.IGNORECASE)
        markdown = re.sub(r"<i>(.*?)</i>", r"_\1_", markdown, flags=re.IGNORECASE)
        markdown = re.sub(
            r'<a\s+href="([^"]+)">(.*?)</a>',
            r"[\2](\1)",
            markdown,
            flags=re.IGNORECASE,
        )
        markdown = re.sub(r"<[^>]+>", "", markdown)
        markdown = html.unescape(markdown).replace("•", "-")
        response = await client.post(
            webhook_url,
            json={
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "contentUrl": None,
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.2",
                            "body": [
                                {
                                    "type": "TextBlock",
                                    "text": markdown,
                                    "wrap": True,
                                }
                            ],
                        },
                    }
                ],
            },
        )
        response.raise_for_status()
        return

    token = await delegated_token(client)
    response = await client.post(
        f"{GRAPH}/chats/{required_env('SOCIAL_TEAMS_CHAT_ID')}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"body": {"contentType": "html", "content": content}},
    )
    response.raise_for_status()


async def run_once(target: date | None = None, send: bool = False) -> dict[str, Any]:
    target = target or (datetime.now(IST).date() + timedelta(days=1))
    # deepseek-v4-flash is a reasoning model — its "thinking" pass over a full
    # workbook prompt can run well past a short timeout, so give it more room
    # than the other (fast, small) requests on this client would need.
    async with httpx.AsyncClient(timeout=180) as client:
        sheets = await read_workbook(client)
        items = await extract_plan_with_ai(client, sheets, target)
        message = format_reminder(items, target)
        if send:
            await send_to_teams(client, message)
    return {"date": target.isoformat(), "count": len(items), "items": items, "html": message}


async def daemon() -> None:
    load_env()
    hour, minute = map(
        int, os.getenv("SOCIAL_REMINDER_TIME_IST", "18:00").split(":", 1)
    )
    last_sent = ""
    try:
        if STATE_FILE.exists():
            last_sent = str(
                json.loads(STATE_FILE.read_text(encoding="utf-8")).get(
                    "last_sent", ""
                )
            )
    except (OSError, ValueError, TypeError):
        logging.warning("Could not read social reminder state; starting fresh")
    logging.info("Social reminder scheduler started for %02d:%02d IST", hour, minute)
    while True:
        now = datetime.now(IST)
        today = now.date().isoformat()
        if (now.hour, now.minute) >= (hour, minute) and last_sent != today:
            try:
                result = await run_once(send=True)
                last_sent = today
                STATE_FILE.write_text(
                    json.dumps(
                        {
                            "last_sent": last_sent,
                            "target_date": result["date"],
                            "sent_at": now.isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                logging.info("Sent %d reminder items for %s", result["count"], result["date"])
            except Exception:
                logging.exception("Social reminder failed; will retry")
        await asyncio.sleep(30)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Print without sending")
    mode.add_argument("--send", action="store_true", help="Send one reminder now")
    mode.add_argument("--daemon", action="store_true", help="Run the daily scheduler")
    parser.add_argument("--date", help="Target date in YYYY-MM-DD; default is tomorrow")
    return parser.parse_args()


async def async_main() -> None:
    load_env()
    args = parse_args()
    if args.daemon:
        await daemon()
        return
    target = date.fromisoformat(args.date) if args.date else None
    result = await run_once(target=target, send=args.send)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
