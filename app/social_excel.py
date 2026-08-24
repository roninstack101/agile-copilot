"""
Social-media content calendar reader.

Resolves the shared calendar workbook (a SharePoint share link, not the
DRIVE_ID/DRIVE_ITEM_ID used for the agile sheet), reads every brand
worksheet, and extracts whatever is planned for a given date.

Each brand worksheet has its own column layout (different platforms, some
have Story columns, some don't) but they all share the same shape:
  - column 0 is a date serial, column 1 is the weekday name
  - 3-5 header rows above the data, with merged cells (platform name spans
    several columns; sub-headers like TYPE/TITLE/CAPTION sit in the row(s)
    below it)
So headers are parsed dynamically per sheet rather than by fixed index.
"""

import base64
import logging
from datetime import date, timedelta

import httpx

from app.config import settings, GRAPH_BASE_URL
from app.graph_auth import graph_auth

logger = logging.getLogger(__name__)

_EXCLUDED_SHEETS = {"updates"}
_PLATFORM_NAMES = {
    "instagram", "linkedin", "youtube", "facebook", "twitter", "x", "threads", "pinterest",
}
# Stems, matched by substring (so "TYPE/CREATIVE LINK" or "CAPTION/COMMENT" still match),
# mapped to the canonical field name used downstream.
_CONTENT_FIELD_ROOTS = {
    "type": "type",
    "title": "title",
    "caption": "caption",
    "stor": "story",
    "desc": "description",
    "note": "note",
    "festival": "festival",
}
_EXCEL_EPOCH = date(1899, 12, 30)


def _encode_share_url(url: str) -> str:
    """Encode a SharePoint sharing URL into a Graph API shareId."""
    b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"u!{b64}"


async def _resolve_drive_item() -> tuple[str, str]:
    """Resolve the configured share link to (drive_id, item_id)."""
    headers = await graph_auth.get_headers()
    share_id = _encode_share_url(settings.SOCIAL_EXCEL_SHARE_URL)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GRAPH_BASE_URL}/shares/{share_id}/driveItem", headers=headers)
        resp.raise_for_status()
        item = resp.json()
    return item["parentReference"]["driveId"], item["id"]


async def _list_calendar_sheets(drive_id: str, item_id: str) -> list[str]:
    headers = await graph_auth.get_headers()
    url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/workbook/worksheets"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        names = [ws["name"] for ws in resp.json().get("value", [])]
    return [n for n in names if n.strip().lower() not in _EXCLUDED_SHEETS]


async def _fetch_sheet_values(drive_id: str, item_id: str, sheet_name: str) -> list[list]:
    headers = await graph_auth.get_headers()
    url = (
        f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}"
        f"/workbook/worksheets/{sheet_name}/usedRange(valuesOnly=true)"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json().get("values", [])


def _is_date_serial(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _excel_serial_to_date(serial) -> date | None:
    try:
        return _EXCEL_EPOCH + timedelta(days=int(serial))
    except (TypeError, ValueError):
        return None


def _find_data_start(values: list[list]) -> int:
    """First row whose column 0 is a date serial — everything above is header."""
    for i, row in enumerate(values):
        if row and _is_date_serial(row[0]):
            return i
    return len(values)


def _forward_fill(row: list) -> list[str]:
    """Undo merged-cell blanking: carry the last non-empty label rightward."""
    filled = []
    last = ""
    for v in row:
        text = str(v).strip() if v not in (None, "") else ""
        if text:
            last = text
        filled.append(last)
    return filled


def _build_column_index(header_rows: list[list]) -> list[dict]:
    """For each column, work out its platform (if any) and content field (if any)."""
    filled_rows = [_forward_fill(r) for r in header_rows]
    num_cols = max((len(r) for r in header_rows), default=0)
    columns = []
    for c in range(num_cols):
        platform = None
        field = None
        for r in filled_rows:
            if c >= len(r) or not r[c]:
                continue
            key = r[c].strip().lower()
            if key in _PLATFORM_NAMES:
                platform = r[c].strip().title()
            else:
                for root, canonical in _CONTENT_FIELD_ROOTS.items():
                    if root in key:
                        field = canonical
                        break
        columns.append({"platform": platform, "field": field})
    return columns


async def get_tomorrow_content(target_date: date | None = None) -> dict:
    """
    Read every brand worksheet and pull out what's planned for `target_date`
    (defaults to tomorrow).

    Returns:
      {
        "date": "2026-08-21", "weekday": "Friday",
        "brands": {
          "ABAJ": {"festival": "", "items": [{"platform": "Instagram", "type": "Video", "title": "..."}]},
          ...
        }
      }
    """
    target = target_date or (date.today() + timedelta(days=1))
    drive_id, item_id = await _resolve_drive_item()
    sheet_names = await _list_calendar_sheets(drive_id, item_id)

    brands: dict[str, dict] = {}
    for sheet_name in sheet_names:
        try:
            values = await _fetch_sheet_values(drive_id, item_id, sheet_name)
        except Exception as e:
            logger.warning("Failed to read social calendar sheet '%s': %s", sheet_name, e)
            # Keep the brand in the message (as "nothing scheduled") instead of
            # silently dropping it — a transient read failure shouldn't make
            # the brand list differ from one run to the next.
            brands[sheet_name.strip()] = {"festival": "", "items": []}
            continue
        if not values:
            continue

        data_start = _find_data_start(values)
        columns = _build_column_index(values[:data_start])

        row = next(
            (r for r in values[data_start:] if r and _excel_serial_to_date(r[0]) == target),
            None,
        )
        if row is None:
            continue

        items_by_platform: dict[str, dict] = {}
        festival = ""
        for c, col in enumerate(columns):
            if c >= len(row) or c < 2:
                continue
            value = row[c]
            if isinstance(value, bool) or value in (None, ""):
                continue
            text = str(value).strip()
            if not text:
                continue

            if col["field"] == "festival":
                festival = text
                continue
            if not col["field"] or not col["platform"]:
                continue

            entry = items_by_platform.setdefault(col["platform"], {})
            entry[col["field"]] = text

        items = [{"platform": p, **fields} for p, fields in items_by_platform.items() if fields]
        brands[sheet_name.strip()] = {"festival": festival, "items": items}

    return {"date": target.isoformat(), "weekday": target.strftime("%A"), "brands": brands}
