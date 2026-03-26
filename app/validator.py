"""
Validation layer — deduplication, backlog matching, defaults, schema enforcement.
Runs after AI parsing and before writing to the Excel sheet.
"""

import logging
from datetime import datetime

from fuzzywuzzy import fuzz

from app.config import (
    ALLOWED_PRIORITIES,
    ALLOWED_STAGES,
    DEFAULT_PRIORITY,
    DEFAULT_STAGE,
    DEFAULT_EXPECTED_SP,
    MIN_SP,
    MAX_SP,
    MAX_FIELD_LENGTH,
    DEDUP_THRESHOLD,
    COLUMN_ORDER,
    ACTIVITY_TYPES,
    KNOWN_BRANDS,
    BRAND_PARENT,
    get_sprint_end_date,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Rule 1: Deduplication
# ──────────────────────────────────────────────


def _task_target_section(task: dict) -> str | None:
    """Infer target section from task brand. Returns lowercase or None."""
    brand = task.get("brand", "").strip().lower()
    return brand if brand else None


def deduplicate(
    tasks: list[dict], existing_rows: list[dict]
) -> tuple[list[dict], list[dict]]:
    """
    Compare parsed tasks against existing sheet rows for the same member.
    Uses fuzzy string matching (Levenshtein ratio >= DEDUP_THRESHOLD).

    Section-aware: when multiple rows match at similar ratios, prefer the one
    in a compatible section (e.g. a brandless task prefers non-brand rows).

    Returns:
        (new_tasks, update_tasks)
        - new_tasks: tasks to append as new rows
        - update_tasks: tasks that match existing rows (include row index for update)
    """
    brand_sections = {b.lower() for b in KNOWN_BRANDS} | {b.lower() for b in BRAND_PARENT}

    new_tasks = []
    update_tasks = []

    for task in tasks:
        task_name = task.get("sprint_backlog", "")
        target_section = _task_target_section(task)

        # Tasks with quantity markers (x1, x2) are explicitly new instances —
        # skip dedup so "Reel shoot x1" today doesn't merge with last week's
        comments = task.get("comments", "")
        if "Quantity:" in comments:
            new_tasks.append(task)
            logger.info("Skipping dedup for '%s' (has quantity marker)", task_name)
            continue

        # Collect all matches above threshold
        # Include Closed/Sent for Approval rows — matching them prevents duplicates.
        # We just won't regress their stage during the merge step.
        candidates = []
        for idx, existing in enumerate(existing_rows):
            existing_name = existing.get("sprint_backlog", "")
            ratio = fuzz.ratio(task_name.lower(), existing_name.lower()) / 100.0
            if ratio >= DEDUP_THRESHOLD:
                candidates.append((ratio, idx, existing))

        if not candidates:
            new_tasks.append(task)
            continue

        # Pick best candidate, preferring section-compatible match
        best_match = None
        for ratio, idx, existing in sorted(candidates, key=lambda c: -c[0]):
            row_section = existing.get("_section", "")

            if target_section:
                # Task has a brand -- prefer match in that brand's section
                if row_section == target_section:
                    best_match = (ratio, idx, existing)
                    break
            else:
                # Brandless task -- prefer match NOT in a brand section
                if row_section not in brand_sections:
                    best_match = (ratio, idx, existing)
                    break

        # Fall back to highest ratio if no section-compatible match
        if not best_match:
            best_match = max(candidates, key=lambda c: c[0])

        best_ratio, row_idx, existing = best_match

        # Merge: update the stage/comments of the existing row
        # Never regress stage: Closed > Sent for Approval > WIP
        merged = {**existing}
        STAGE_RANK = {"WIP": 0, "Sent for Approval": 1, "Closed": 2}
        existing_stage = existing.get("stage", DEFAULT_STAGE)
        new_stage = task.get("stage", DEFAULT_STAGE)
        if STAGE_RANK.get(new_stage, 0) >= STAGE_RANK.get(existing_stage, 0):
            merged["stage"] = new_stage
        else:
            merged["stage"] = existing_stage
        merged["priority"] = task.get("priority", existing.get("priority", DEFAULT_PRIORITY))
        if task.get("comments"):
            old_comments = existing.get("comments", "")
            if old_comments:
                merged["comments"] = f"{old_comments}; Updated: {task['comments']}"
            else:
                merged["comments"] = task["comments"]
        if task.get("dependency"):
            merged["dependency"] = task["dependency"]
        merged["_row_index"] = row_idx
        if "_sheet_row" in existing:
            merged["_sheet_row"] = existing["_sheet_row"]
        update_tasks.append(merged)
        logger.info(
            "Dedup match: '%s' ~ '%s' (%.0f%%, section=%s)",
            task_name,
            existing.get("sprint_backlog"),
            best_ratio * 100,
            existing.get("_section", "?"),
        )

    logger.info(
        "Dedup result: %d new, %d updates", len(new_tasks), len(update_tasks)
    )
    return new_tasks, update_tasks


# ──────────────────────────────────────────────
# Rule 2: Backlog matching
# ──────────────────────────────────────────────


def match_backlog(tasks: list[dict], backlog: list[str]) -> list[dict]:
    """
    If a parsed task fuzzy-matches a backlog item, copy the exact backlog name
    and add 'From backlog' to comments.

    Uses fuzz.ratio (full string similarity) not partial_ratio, to avoid
    short backlog items like "Catalogue" matching long task names like
    "Schneider Catalogue changes".
    """
    if not backlog:
        return tasks

    for task in tasks:
        task_name = task.get("sprint_backlog", "")
        best_match = None
        best_ratio = 0.0

        for item in backlog:
            # Use ratio for similar-length strings, partial_ratio for longer task names.
            # But guard against short backlog items matching long tasks spuriously:
            # require the backlog item to be at least 40% of the task name length.
            task_lower = task_name.lower()
            item_lower = item.lower()
            if len(item_lower) < len(task_lower) * 0.4:
                continue  # backlog item too short relative to task name
            ratio = fuzz.ratio(task_lower, item_lower) / 100.0
            partial = fuzz.partial_ratio(task_lower, item_lower) / 100.0
            score = max(ratio, partial * 0.9)  # slight penalty for partial matches
            if score > best_ratio:
                best_ratio = score
                best_match = item

        if best_match and best_ratio >= DEDUP_THRESHOLD:
            # Don't write to backlog column — the backlog item already exists in its own row
            comments = task.get("comments", "")
            if "From backlog" not in comments:
                task["comments"] = f"From backlog; {comments}" if comments else "From backlog"

    return tasks


# ──────────────────────────────────────────────
# Rule 3: Field defaults
# ──────────────────────────────────────────────


def apply_defaults(tasks: list[dict], sprint_end: str | None = None) -> list[dict]:
    """Enforce correct field types, defaults, and ranges."""
    sprint_end = sprint_end or get_sprint_end_date()

    for task in tasks:
        # Priority
        if task.get("priority") not in ALLOWED_PRIORITIES:
            task["priority"] = DEFAULT_PRIORITY

        # Stage
        if task.get("stage") not in ALLOWED_STAGES:
            task["stage"] = DEFAULT_STAGE

        # Deadline
        deadline = task.get("deadline", "")
        if not deadline:
            task["deadline"] = sprint_end
        else:
            # Validate date format
            try:
                datetime.fromisoformat(deadline)
            except (ValueError, TypeError):
                task["deadline"] = sprint_end

        # Expected story points
        try:
            sp = int(task.get("expected_story_points", DEFAULT_EXPECTED_SP))
            task["expected_story_points"] = max(MIN_SP, min(MAX_SP, sp))
        except (ValueError, TypeError):
            task["expected_story_points"] = DEFAULT_EXPECTED_SP

        # Actual story points: always 0 on creation
        task["actual_story_points"] = 0

        # Sprint backlog must not be empty
        if not task.get("sprint_backlog", "").strip():
            task["sprint_backlog"] = "Untitled task"

    return tasks


# ──────────────────────────────────────────────
# Rule 4: Adhoc verification
# ──────────────────────────────────────────────


def verify_adhoc(tasks: list[dict]) -> list[dict]:
    """
    If a task is flagged as adhoc but matches a backlog item,
    remove the adhoc flag — it was pre-assigned work.
    """
    for task in tasks:
        comments = task.get("comments", "")
        if "Adhoc task" in comments and task.get("backlog"):
            task["comments"] = comments.replace("Adhoc task", "").strip("; ").strip()
            if "From backlog" not in task["comments"]:
                task["comments"] = (
                    f"From backlog; {task['comments']}" if task["comments"] else "From backlog"
                )
    return tasks


# ──────────────────────────────────────────────
# Rule 5: Dependency normalization
# ──────────────────────────────────────────────


def normalize_dependencies(tasks: list[dict]) -> list[dict]:
    """Standardize dependency text to 'Team/Person – description' format."""
    for task in tasks:
        dep = task.get("dependency", "")
        if dep:
            dep = dep.strip()
            # Capitalize first letter of each word before '–' or '-'
            parts = dep.split("–") if "–" in dep else dep.split("-", 1)
            if len(parts) >= 2:
                team = parts[0].strip().title()
                desc = parts[1].strip()
                dep = f"{team} – {desc}"
            task["dependency"] = dep
    return tasks


# ──────────────────────────────────────────────
# Rule 7: Schema enforcement
# ──────────────────────────────────────────────


def enforce_schema(tasks: list[dict]) -> list[dict]:
    """
    Ensure every task has exactly the required fields, correct types,
    and no field exceeds MAX_FIELD_LENGTH characters.
    """
    clean_tasks = []
    string_fields = [
        "brand", "activity_type", "backlog", "sprint_backlog", "dependency",
        "deadline", "priority", "comments",
    ]
    int_fields = ["expected_story_points", "actual_story_points"]

    for task in tasks:
        clean = {}

        for field in string_fields:
            val = str(task.get(field, ""))[:MAX_FIELD_LENGTH]
            clean[field] = val

        for field in int_fields:
            try:
                clean[field] = int(task.get(field, 0))
            except (ValueError, TypeError):
                clean[field] = 0

        # Keep stage as a string (excel_writer handles column expansion)
        stage = task.get("stage", DEFAULT_STAGE)
        if stage not in ALLOWED_STAGES:
            stage = DEFAULT_STAGE
        clean["stage"] = stage

        # Validate activity_type against allowed values
        if clean["activity_type"] and clean["activity_type"] not in ACTIVITY_TYPES:
            clean["activity_type"] = ""

        # Preserve internal fields
        if "_row_index" in task:
            clean["_row_index"] = task["_row_index"]
        if "_sheet_row" in task:
            clean["_sheet_row"] = task["_sheet_row"]

        clean_tasks.append(clean)

    return clean_tasks


def task_to_row(task: dict) -> list:
    """
    Convert a validated task dict to a list of values matching COLUMN_ORDER.
    This is what gets written to the Excel sheet.
    """
    return [
        task.get("brand", ""),
        task.get("activity_type", ""),
        task.get("backlog", ""),
        task.get("sprint_backlog", ""),
        task.get("dependency", ""),
        task.get("deadline", ""),
        task.get("priority", DEFAULT_PRIORITY),
        task.get("WIP", ""),
        task.get("Sent for Approval", ""),
        task.get("Closed", ""),
        task.get("comments", ""),
        task.get("expected_story_points", DEFAULT_EXPECTED_SP),
        task.get("actual_story_points", 0),
    ]


# ──────────────────────────────────────────────
# Main validation pipeline
# ──────────────────────────────────────────────


def validate_all(
    tasks: list[dict],
    existing_rows: list[dict],
    backlog: list[str],
    sprint_end: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Run all validation rules in order:
      1. Backlog matching
      2. Adhoc verification
      3. Dependency normalization
      4. Deduplication
      5. Defaults
      6. Schema enforcement

    Returns:
        (new_tasks, update_tasks)
        - new_tasks: list of validated task dicts to append
        - update_tasks: list of validated task dicts with _row_index to update
    """
    # Steps 1–3: enrich tasks
    tasks = match_backlog(tasks, backlog)
    tasks = verify_adhoc(tasks)
    tasks = normalize_dependencies(tasks)

    # Step 4: skip task-level dedup — every EOD entry is a new task.
    # Duplicate notifications are already handled at the Graph notification level
    # (_processed_messages cache in main.py).
    new_tasks = tasks
    update_tasks = []

    # Step 5: apply defaults
    new_tasks = apply_defaults(new_tasks, sprint_end)

    # Step 6: schema enforcement
    new_tasks = enforce_schema(new_tasks)

    logger.info(
        "Validation complete: %d new tasks",
        len(new_tasks),
    )
    return new_tasks, update_tasks
