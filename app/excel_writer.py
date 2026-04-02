"""
Excel writer — reads and writes the agile sheet via Microsoft Graph API.
Supports per-member worksheets with brand-grouped sections and variable column layouts.
"""

import logging
from typing import Any

import httpx

from app.config import settings, GRAPH_BASE_URL, COLUMN_ORDER, KNOWN_BRANDS, BRAND_PARENT
from app.graph_auth import graph_auth

logger = logging.getLogger(__name__)


def _workbook_url() -> str:
    """Build the base Graph API URL for the workbook."""
    return f"{GRAPH_BASE_URL}/drives/{settings.DRIVE_ID}/items/{settings.DRIVE_ITEM_ID}/workbook"


# ──────────────────────────────────────────────
# Sheet resolution
# ──────────────────────────────────────────────


async def resolve_sheet_name(member_name: str) -> str:
    """
    Resolve the worksheet name for a given team member.

    Strategy:
      1. List all worksheets in the workbook
      2. Find the one whose name best matches the member_name
      3. Fall back to settings.SHEET_NAME if no match found
    """
    if not member_name or member_name == "Unknown":
        return settings.SHEET_NAME

    try:
        headers = await graph_auth.get_headers()
        url = f"{_workbook_url()}/worksheets"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

        EXCLUDED_MEMBERS = {"harshil"}
        sheets = [ws["name"] for ws in data.get("value", [])]
        member_lower = member_name.strip().lower()

        if member_lower.split()[0] in EXCLUDED_MEMBERS:
            logger.info("Member '%s' is excluded — skipping", member_name)
            return None

        # Exact match (case-insensitive)
        for name in sheets:
            if name.strip().lower() == member_lower:
                logger.info("Sheet exact match: '%s' → '%s'", member_name, name)
                return name

        # Partial match
        for name in sheets:
            name_lower = name.strip().lower()
            if member_lower in name_lower or name_lower in member_lower:
                logger.info("Sheet partial match: '%s' → '%s'", member_name, name)
                return name

        # First name match
        member_first = member_lower.split()[0] if member_lower.split() else ""
        for name in sheets:
            sheet_first = name.strip().lower().split()[0] if name.strip() else ""
            if member_first and sheet_first == member_first:
                logger.info("Sheet first-name match: '%s' → '%s'", member_name, name)
                return name

        logger.warning("No sheet found for '%s'", member_name)
        return None

    except Exception as e:
        logger.error("Failed to list worksheets: %s", e)
        return None


async def list_all_sheets() -> list[str]:
    """List all worksheet names in the workbook, excluding utility sheets."""
    EXCLUDE = {"sheet1", "initiatives", "template", "harshil"}
    try:
        headers = await graph_auth.get_headers()
        url = f"{_workbook_url()}/worksheets"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        sheets = [ws["name"] for ws in data.get("value", [])]
        return [s for s in sheets if s.strip().lower() not in EXCLUDE]
    except Exception as e:
        logger.error("Failed to list worksheets: %s", e)
        return []


# ──────────────────────────────────────────────
# Read operations
# ──────────────────────────────────────────────


async def read_sheet(sheet_name: str | None = None) -> list[list[Any]]:
    """Read the entire used range of the worksheet."""
    sheet = sheet_name or settings.SHEET_NAME
    headers = await graph_auth.get_headers()
    url = f"{_workbook_url()}/worksheets/{sheet}/usedRange(valuesOnly=true)"

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

    values = data.get("values") or []
    logger.info("Read %d rows from sheet '%s'", len(values), sheet)
    return values


def _detect_header_row(values: list[list[Any]]) -> tuple[int, list[str]]:
    """
    Detect the header row in the sheet by looking for known column names.
    Returns (header_row_index, header_list).
    """
    known_headers = {"backlog", "sprint backlog", "dependency", "dependancy",
                     "deadline", "priority", "wip", "comments"}

    for idx, row in enumerate(values):
        row_lower = {str(cell).strip().lower() for cell in row if cell}
        matches = row_lower & known_headers
        if len(matches) >= 3:  # at least 3 known headers
            header = [str(cell).strip() if cell else "" for cell in row]
            return idx, header

    # Fallback: assume first row is header
    if values:
        return 0, [str(cell).strip() if cell else "" for cell in values[0]]
    return 0, []


def _build_column_map(header: list[str]) -> dict[str, int]:
    """
    Build a mapping from normalized field names to column indices.
    Handles variations like 'Dependancy' vs 'Dependency'.
    """
    col_map = {}
    for idx, col_name in enumerate(header):
        lower = col_name.lower().strip()

        if lower in ("brand",):
            col_map["brand"] = idx
        elif lower in ("activity type",):
            col_map["activity_type"] = idx
        elif lower in ("backlog",):
            col_map["backlog"] = idx
        elif lower in ("sprint backlog",):
            col_map["sprint_backlog"] = idx
        elif lower in ("dependency", "dependancy"):
            col_map["dependency"] = idx
        elif lower in ("deadline",):
            col_map["deadline"] = idx
        elif lower in ("priority",):
            col_map["priority"] = idx
        elif lower in ("wip",):
            col_map["wip"] = idx
        elif lower in ("sent for approval",):
            col_map["sent_for_approval"] = idx
        elif lower in ("stuck in approval / dependancy", "stuck in approval"):
            col_map["stuck"] = idx
        elif lower in ("closed",):
            col_map["closed"] = idx
        elif lower in ("comments", "comments / outcome"):
            col_map["comments"] = idx
        elif lower in ("expected story points",):
            col_map["expected_sp"] = idx
        elif lower in ("actual story points",):
            col_map["actual_sp"] = idx
        elif lower in ("id",):
            col_map["id"] = idx
        elif lower in ("today",):
            col_map["today"] = idx

    return col_map



def _extract_existing_rows(values: list[list[Any]], header_idx: int, header: list[str], col_map: dict) -> list[dict]:
    """Extract task rows from cached sheet values. Returns list of task dicts."""
    rows = []

    def _get_str(padded, field):
        idx = col_map.get(field, -1)
        if idx >= 0 and idx < len(padded):
            return str(padded[idx]).strip() if padded[idx] else ""
        return ""

    for row_idx in range(header_idx + 1, len(values)):
        row = values[row_idx]
        padded = list(row) + [""] * (len(header) - len(row))

        if not any(str(cell).strip() for cell in padded if cell):
            continue

        mapped = {
            "brand": _get_str(padded, "brand"),
            "activity_type": _get_str(padded, "activity_type"),
            "backlog": _get_str(padded, "backlog"),
            "sprint_backlog": _get_str(padded, "sprint_backlog"),
            "dependency": _get_str(padded, "dependency"),
            "deadline": _get_str(padded, "deadline"),
            "priority": _get_str(padded, "priority"),
            "stage": _infer_stage_from_row(padded, col_map),
            "comments": _get_str(padded, "comments"),
            "expected_story_points": _safe_int(padded[col_map["expected_sp"]] if "expected_sp" in col_map and col_map["expected_sp"] < len(padded) else 0),
            "actual_story_points": _safe_int(padded[col_map["actual_sp"]] if "actual_sp" in col_map and col_map["actual_sp"] < len(padded) else 0),
        }

        if mapped["sprint_backlog"]:
            mapped["_sheet_row"] = row_idx
            rows.append(mapped)

    return rows


async def get_existing_rows(member_name: str | None = None, sheet_name: str | None = None) -> list[dict]:
    """Read the member's sheet and return task rows as list of dicts."""
    values = await read_sheet(sheet_name)
    if not values:
        return []

    header_idx, header = _detect_header_row(values)
    col_map = _build_column_map(header)
    return _extract_existing_rows(values, header_idx, header, col_map)


def _infer_stage_from_row(row: list, col_map: dict) -> str:
    """Infer stage from WIP/Sent for Approval/Closed columns."""
    def _get(field):
        idx = col_map.get(field, -1)
        if idx >= 0 and idx < len(row):
            val = str(row[idx]).strip().lower()
            return val in ("true", "yes", "1")
        return False

    if _get("closed"):
        return "Closed"
    if _get("sent_for_approval"):
        return "Sent for Approval"
    return "WIP"


def _safe_int(val: Any) -> int:
    """Safely convert a value to int."""
    try:
        if val is None or str(val).strip() in ("", "None", "False"):
            return 0
        return int(float(str(val)))
    except (ValueError, TypeError):
        return 0


def _last_data_row(values: list[list[Any]], header_idx: int) -> int:
    """
    Return the 1-indexed row number of the last row that contains actual data.
    Scans backwards to skip trailing blank rows that appear in usedRange due
    to cell formatting (e.g. Shaily's sheet has formatted-but-empty rows 47-49).
    """
    for row_idx in range(len(values) - 1, header_idx, -1):
        row = values[row_idx]
        if any(cell is not None and str(cell).strip() not in ("", "None", "False", "TRUE", "FALSE") for cell in row):
            return row_idx + 1  # convert 0-indexed to 1-indexed
    return header_idx + 1  # fallback: right after header row


def _extract_backlog_with_positions(
    values: list[list[Any]], header_idx: int, col_map: dict
) -> list[dict]:
    """
    Extract backlog items with their cell positions from cached sheet values.
    Returns: [{"text": "Brand Identity Setup", "row_idx": 5, "col_idx": 0}, ...]
    """
    bl_idx = col_map.get("backlog")
    if bl_idx is None:
        return []

    brand_names_lower = {b.lower() for b in KNOWN_BRANDS} | {b.lower() for b in BRAND_PARENT}
    items = []
    seen = set()

    for row_idx in range(header_idx + 1, len(values)):
        row = values[row_idx]
        if bl_idx < len(row):
            val = str(row[bl_idx]).strip() if row[bl_idx] else ""
            if val and val.lower() not in brand_names_lower and val.lower() not in seen:
                seen.add(val.lower())
                items.append({"text": val, "row_idx": row_idx, "col_idx": bl_idx})

    return items


def _extract_backlog_list(
    values: list[list[Any]], header_idx: int, col_map: dict
) -> list[str]:
    """Extract backlog item names from cached sheet values. Returns list[str]."""
    return [item["text"] for item in _extract_backlog_with_positions(values, header_idx, col_map)]


async def get_backlog(member_name: str | None = None, sheet_name: str | None = None) -> list[str]:
    """Extract backlog items from the member's sheet."""
    values = await read_sheet(sheet_name)
    if not values:
        return []

    header_idx, header = _detect_header_row(values)
    col_map = _build_column_map(header)
    return _extract_backlog_list(values, header_idx, col_map)



async def read_sheet_context(sheet_name: str | None = None) -> dict:
    """
    Read the sheet once and return all derived context.
    Avoids multiple Graph API calls by extracting everything from a single read.
    """
    sheet = sheet_name or settings.SHEET_NAME
    values = await read_sheet(sheet)
    if not values:
        return {
            "values": [], "header_idx": 0, "header": [], "col_map": {},
            "backlog_items": [], "existing_rows": [], "backlog_list": [],
        }

    header_idx, header = _detect_header_row(values)
    col_map = _build_column_map(header)

    return {
        "values": values,
        "header_idx": header_idx,
        "header": header,
        "col_map": col_map,
        "backlog_items": _extract_backlog_with_positions(values, header_idx, col_map),
        "existing_rows": _extract_existing_rows(values, header_idx, header, col_map),
        "backlog_list": _extract_backlog_list(values, header_idx, col_map),
    }


async def clear_backlog_cell(sheet_name: str, excel_row: int, col_idx: int) -> None:
    """PATCH a single cell to empty string, clearing the backlog entry after promotion."""
    col_letter = chr(ord("A") + col_idx)
    address = f"{col_letter}{excel_row}:{col_letter}{excel_row}"

    headers = await graph_auth.get_headers()
    url = f"{_workbook_url()}/worksheets/{sheet_name}/range(address='{address}')"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(url, headers=headers, json={"values": [[""]]})
        resp.raise_for_status()

    logger.info("Cleared backlog cell %s%d in sheet '%s'", col_letter, excel_row, sheet_name)


async def promote_backlog_cell(
    sheet_name: str, excel_row: int, backlog_col: int, sprint_col: int, header_name: str
) -> None:
    """
    Move a backlog item to Sprint Backlog: clear the Backlog cell and write
    the header name into the Sprint Backlog cell of the same row.
    """
    bl_letter = chr(ord("A") + backlog_col)
    sb_letter = chr(ord("A") + sprint_col)

    # PATCH both cells: clear backlog, write sprint_backlog
    address = f"{bl_letter}{excel_row}:{sb_letter}{excel_row}"
    num_cols = sprint_col - backlog_col + 1
    row_values = [""] * num_cols
    row_values[0] = ""              # Backlog = clear
    row_values[-1] = header_name    # Sprint Backlog = header name

    headers = await graph_auth.get_headers()
    url = f"{_workbook_url()}/worksheets/{sheet_name}/range(address='{address}')"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(url, headers=headers, json={"values": [row_values]})
        resp.raise_for_status()

    logger.info(
        "Promoted backlog: cleared %s%d, wrote '%s' to %s%d in sheet '%s'",
        bl_letter, excel_row, header_name, sb_letter, excel_row, sheet_name,
    )


async def update_backlog_row(
    sheet_name: str, task: dict, backlog_row_idx: int, backlog_col_idx: int
) -> None:
    """
    Write task data onto the same row as a backlog item.
    Clears the Backlog cell and fills Sprint Backlog + other columns on that row.
    """
    values = await read_sheet(sheet_name)
    if not values:
        raise ValueError(f"Sheet '{sheet_name}' is empty")

    header_idx, header = _detect_header_row(values)
    col_map = _build_column_map(header)
    num_cols = len(header)

    row_values = _task_to_row(task, col_map, num_cols)

    # Clear the backlog cell in the row
    if "backlog" in col_map:
        row_values[col_map["backlog"]] = ""

    excel_row = backlog_row_idx + 1  # 0-indexed → 1-indexed
    await _write_row(sheet_name, excel_row, row_values, num_cols)

    # Apply formatting based on task stage
    end_col = chr(ord("A") + num_cols - 1)
    address = f"A{excel_row}:{end_col}{excel_row}"
    fill_color = _row_fill_color(task)
    headers = await graph_auth.get_headers()
    format_url = f"{_workbook_url()}/worksheets/{sheet_name}/range(address='{address}')/format"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(format_url, headers=headers, json={
            "fill": {"color": fill_color},
            "font": {"bold": False, "color": "#000000", "italic": False},
        })
        if resp.status_code >= 400:
            logger.debug("Format apply returned %d — continuing", resp.status_code)

    logger.info(
        "Backlog in-place update: wrote '%s' on row %d (fill=%s) in sheet '%s'",
        task.get("sprint_backlog", ""), excel_row, fill_color, sheet_name,
    )


# ──────────────────────────────────────────────
# Write operations (range-based, no Tables)
# ──────────────────────────────────────────────


def _task_to_row(task: dict, col_map: dict, num_cols: int) -> list:
    """
    Convert a task dict to a row list matching the sheet's column layout.
    Uses the column map to place values in the correct positions.
    """
    row: list[Any] = [""] * num_cols

    # Map task fields to columns
    if "brand" in col_map:
        row[col_map["brand"]] = task.get("brand", "")
    if "activity_type" in col_map:
        row[col_map["activity_type"]] = task.get("activity_type", "")
    if "backlog" in col_map:
        row[col_map["backlog"]] = task.get("backlog", "")
    if "sprint_backlog" in col_map:
        row[col_map["sprint_backlog"]] = task.get("sprint_backlog", "")
    if "dependency" in col_map:
        row[col_map["dependency"]] = task.get("dependency", "")
    if "deadline" in col_map:
        row[col_map["deadline"]] = task.get("deadline", "")
    if "priority" in col_map:
        row[col_map["priority"]] = task.get("priority", "Medium")

    # Stage → expand into WIP / Sent for Approval / Closed columns
    # Write "" for inactive columns (blank = unchecked) so rows match existing sheet style
    stage = task.get("stage", "WIP")
    if "wip" in col_map:
        row[col_map["wip"]] = True if stage == "WIP" else ""
    if "sent_for_approval" in col_map:
        row[col_map["sent_for_approval"]] = True if stage == "Sent for Approval" else ""
    if "closed" in col_map:
        row[col_map["closed"]] = True if stage == "Closed" else ""

    if "comments" in col_map:
        row[col_map["comments"]] = task.get("comments", "")
    if "expected_sp" in col_map:
        row[col_map["expected_sp"]] = task.get("expected_story_points", 2)
    if "actual_sp" in col_map:
        row[col_map["actual_sp"]] = task.get("actual_story_points", 0)

    return row


async def write_tasks(
    new_tasks: list[dict], update_tasks: list[dict] | None = None,
    sheet_name: str | None = None
) -> dict:
    """
    Write validated tasks to the member's Excel sheet.
    Uses range-based writing — flat append at the end of the sheet.

    Args:
        new_tasks: list of task dicts to append
        update_tasks: list of task dicts with _row_index to update (optional)
        sheet_name: the member's worksheet name

    Returns a summary dict.
    """
    sheet = sheet_name or settings.SHEET_NAME
    results = {"appended": 0, "updated": 0, "errors": []}

    if not new_tasks and not update_tasks:
        return results

    # Read current sheet to understand column layout
    try:
        values = await read_sheet(sheet)
    except Exception as e:
        logger.error("Failed to read sheet '%s': %s", sheet, e)
        results["errors"].append(f"Read failed: {e}")
        return results

    if not values:
        logger.warning("Sheet '%s' is empty", sheet)
        results["errors"].append(f"Sheet '{sheet}' is empty — cannot determine column layout")
        return results

    header_idx, header = _detect_header_row(values)
    col_map = _build_column_map(header)
    num_cols = len(header)

    if not col_map.get("sprint_backlog"):
        results["errors"].append("Cannot find 'Sprint Backlog' column in sheet")
        return results

    # ── Execute updates FIRST (before inserts shift rows) ──
    if update_tasks:
        for task in update_tasks:
            sheet_row = task.get("_sheet_row")
            if sheet_row is not None:
                try:
                    row_values = _task_to_row(task, col_map, num_cols)
                    actual_row = sheet_row + 1  # 0-indexed → 1-indexed
                    await _write_row(sheet, actual_row, row_values, num_cols)
                    results["updated"] += 1
                except Exception as e:
                    logger.error("Failed to update row %d in '%s': %s", sheet_row, sheet, e)
                    results["errors"].append(f"Update row {sheet_row} failed: {e}")
            else:
                row_idx = task.get("_row_index")
                if row_idx is not None:
                    try:
                        row_values = _task_to_row(task, col_map, num_cols)
                        actual_row = header_idx + 1 + row_idx + 1
                        await _write_row(sheet, actual_row, row_values, num_cols)
                        results["updated"] += 1
                    except Exception as e:
                        logger.error("Failed to update row %d in '%s': %s", row_idx, sheet, e)
                        results["errors"].append(f"Update row {row_idx} failed: {e}")

    # ── Build brand→last_row map for grouping ──
    brand_col = col_map.get("brand")
    brand_last_row: dict[str, int] = {}  # brand_lower → last 1-indexed row
    if brand_col is not None:
        for row_idx in range(header_idx + 1, len(values)):
            row = values[row_idx]
            if brand_col < len(row) and row[brand_col]:
                brand_val = str(row[brand_col]).strip().lower()
                if brand_val:
                    brand_last_row[brand_val] = row_idx + 1  # 1-indexed

    # Track how many rows we've inserted per brand (shifts subsequent positions)
    rows_inserted: dict[str, int] = {}
    total_inserted = 0

    # ── Insert new tasks grouped by brand ──
    # Use last non-empty row (not len(values)) to skip trailing blank/formatted rows
    end_of_sheet = _last_data_row(values, header_idx) + 1  # 1-indexed, after last data row

    for task in new_tasks:
        try:
            row_values = _task_to_row(task, col_map, num_cols)
            task_brand = task.get("brand", "").strip().lower()
            if task_brand in ("unknown", "n/a", "none"):
                task_brand = ""

            if task_brand and task_brand in brand_last_row:
                # Insert after the last row of this brand
                base_pos = brand_last_row[task_brand]
                # Adjust for rows we've already inserted above or at this position
                offset = sum(
                    cnt for b, cnt in rows_inserted.items()
                    if brand_last_row.get(b, end_of_sheet) <= base_pos
                )
                insert_pos = base_pos + offset + 1  # +1 = after the last row

                await _insert_and_write_row(sheet, insert_pos, row_values, num_cols, task=task)
                rows_inserted[task_brand] = rows_inserted.get(task_brand, 0) + 1
                # Update the last row for this brand so next same-brand task goes below
                brand_last_row[task_brand] = base_pos + rows_inserted[task_brand]
                total_inserted += 1
            else:
                # No existing brand rows → append at end
                append_pos = end_of_sheet + total_inserted
                await _insert_and_write_row(sheet, append_pos, row_values, num_cols, task=task)
                if task_brand:
                    brand_last_row[task_brand] = append_pos
                    rows_inserted[task_brand] = rows_inserted.get(task_brand, 0) + 1
                total_inserted += 1

            results["appended"] += 1
        except Exception as e:
            logger.error("Failed to insert task '%s' in '%s': %s",
                         task.get("sprint_backlog", ""), sheet, e)
            results["errors"].append(f"Insert failed: {e}")

    logger.info(
        "Write complete for '%s': %d appended, %d updated, %d errors",
        sheet, results["appended"], results["updated"], len(results["errors"]),
    )
    return results


def _row_fill_color(task: dict | None) -> str:
    """Determine row background color based on task stage/type."""
    if not task:
        return "#FFFFFF"

    stage = task.get("stage", "WIP")
    comments = task.get("comments", "")

    if stage == "Closed":
        return "#C6EFCE"  # light green
    if "Adhoc task" in comments:
        return "#FFC7CE"  # light red
    return "#D9D9D9"      # light grey (WIP / default)


async def _insert_and_write_row(
    sheet_name: str, excel_row: int, values: list, num_cols: int, task: dict | None = None
) -> dict:
    """Insert a new row at the given position and write values to it."""
    import asyncio

    end_col = chr(ord("A") + num_cols - 1)
    address = f"A{excel_row}:{end_col}{excel_row}"
    base = f"{_workbook_url()}/worksheets/{sheet_name}/range(address='{address}')"
    fill_color = _row_fill_color(task)

    headers = await graph_auth.get_headers()

    # Step 1: Insert a blank row (must complete before writing values)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{base}/insert", headers=headers, json={"shift": "Down"})
        resp.raise_for_status()

    # Step 2: Write values + all formatting in parallel (independent of each other)
    async def _patch(url: str, body: dict) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(url, headers=headers, json=body)
            if r.status_code >= 400:
                logger.debug("PATCH %s returned %d", url.split("/range")[1], r.status_code)

    results = await asyncio.gather(
        _patch(base, {"values": [values]}),
        _patch(f"{base}/format/fill", {"color": fill_color}),
        _patch(f"{base}/format/font", {"bold": False, "color": "#000000", "italic": False, "size": 11, "name": "Calibri"}),
        _patch(f"{base}/format", {"wrapText": True, "horizontalAlignment": "Center", "verticalAlignment": "Center"}),
        return_exceptions=True,
    )

    # Log any unexpected errors but don't fail the whole write
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Row %d format/write error (non-fatal): %s", excel_row, r)

    logger.info("Inserted and wrote row %d in sheet '%s' (fill=%s)", excel_row, sheet_name, fill_color)
    return {}


async def _write_row(sheet_name: str, excel_row: int, values: list, num_cols: int) -> dict:
    """Overwrite an existing row (for updates only)."""
    end_col = chr(ord("A") + num_cols - 1)
    address = f"A{excel_row}:{end_col}{excel_row}"

    headers = await graph_auth.get_headers()
    url = f"{_workbook_url()}/worksheets/{sheet_name}/range(address='{address}')"

    payload = {"values": [values]}

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.patch(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()

    logger.info("Wrote row %d in sheet '%s'", excel_row, sheet_name)
    return result
