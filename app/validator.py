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


async def deduplicate(
    tasks: list[dict], existing_rows: list[dict]
) -> tuple[list[dict], list[dict]]:
    """
    Compare parsed tasks against existing sheet rows.
    Uses semantic similarity (embeddings) with fuzzy fallback.

    Returns:
        (new_tasks, update_tasks)
    """
    from app.embeddings import find_best_match

    brand_sections = {b.lower() for b in KNOWN_BRANDS} | {b.lower() for b in BRAND_PARENT}
    existing_names = [r.get("sprint_backlog", "") for r in existing_rows]

    new_tasks = []
    update_tasks = []

    for task in tasks:
        task_name = task.get("sprint_backlog", "")
        update_idx = task.get("update_row_idx", -1)

        # Priority 1: AI-driven match (LLM chose an existing row)
        existing = None
        if update_idx != -1:
            existing = next((r for r in existing_rows if r.get("_sheet_row") == update_idx), None)
            if existing:
                logger.info("AI matched task '%s' to row %d", task_name, update_idx)
                # Found a match via AI index
                row_idx = next((i for i, r in enumerate(existing_rows) if r.get("_sheet_row") == update_idx), -1)
            else:
                logger.warning("AI suggested row index %d but it wasn't found in existing_rows", update_idx)

        # Priority 2: Fallback to Semantic/Fuzzy match (if AI suggested -1 or was wrong)
        if not existing:
            # --- Semantic match ---
            matched_name, score = await find_best_match(task_name, existing_names, threshold=0.85)

            # --- Fuzzy fallback ---
            if matched_name is None:
                best_ratio = 0.0
                for name in existing_names:
                    ratio = fuzz.ratio(task_name.lower(), name.lower()) / 100.0
                    token_ratio = fuzz.token_set_ratio(task_name.lower(), name.lower()) / 100.0
                    s = max(ratio, token_ratio * 0.95)
                    if s > best_ratio:
                        best_ratio = s
                        matched_name = name if s >= DEDUP_THRESHOLD else None
                score = best_ratio

            if matched_name:
                candidates = [(i, r) for i, r in enumerate(existing_rows) if r.get("sprint_backlog") == matched_name]
                if candidates:
                    target_section = _task_target_section(task)
                    row_idx, existing = candidates[0]
                    # Section-aware refinement
                    for i, r in candidates:
                        row_section = r.get("brand", "").strip().lower()
                        if target_section and row_section == target_section:
                            row_idx, existing = i, r
                            break
                    logger.info("Fallback match: '%s' → '%s' (score=%.2f)", task_name, existing.get("sprint_backlog"), score)

        if not existing:
            new_tasks.append(task)
            continue

        # Merge — never regress stage
        STAGE_RANK = {"WIP": 0, "Sent for Approval": 1, "Closed": 2}
        merged = {**existing}
        existing_stage = existing.get("stage", DEFAULT_STAGE)
        new_stage = task.get("stage", DEFAULT_STAGE)
        merged["stage"] = (
            new_stage
            if STAGE_RANK.get(new_stage, 0) >= STAGE_RANK.get(existing_stage, 0)
            else existing_stage
        )
        merged["priority"] = task.get("priority", existing.get("priority", DEFAULT_PRIORITY))
        
        # Clean up existing comments if it was just "From backlog" and we have new info
        old_comments = existing.get("comments", "")
        new_comments = task.get("comments", "")
        if new_comments:
            if not old_comments or old_comments == "From backlog":
                merged["comments"] = new_comments
            elif new_comments not in old_comments:
                merged["comments"] = f"{old_comments}; {new_comments}"
        
        if task.get("dependency"):
            merged["dependency"] = task["dependency"]
        
        merged["_row_index"] = row_idx
        if "_sheet_row" in existing:
            merged["_sheet_row"] = existing["_sheet_row"]

        update_tasks.append(merged)

    logger.info("Dedup result: %d new, %d updates", len(new_tasks), len(update_tasks))
    return new_tasks, update_tasks


# ──────────────────────────────────────────────
# Rule 2: Backlog matching
# ──────────────────────────────────────────────


async def match_backlog(tasks: list[dict], backlog: list[str]) -> list[dict]:
    """
    If a parsed task semantically matches a backlog item, tag it with 'From backlog'.
    Uses semantic similarity with fuzzy fallback.
    """
    from app.embeddings import find_best_match

    if not backlog:
        return tasks

    for task in tasks:
        task_name = task.get("sprint_backlog", "")

        # Semantic match
        matched, score = await find_best_match(task_name, backlog, threshold=0.80)

        # Fuzzy fallback
        if matched is None:
            best_ratio = 0.0
            for item in backlog:
                task_lower = task_name.lower()
                item_lower = item.lower()
                if len(item_lower) < len(task_lower) * 0.4:
                    continue
                ratio = fuzz.ratio(task_lower, item_lower) / 100.0
                partial = fuzz.partial_ratio(task_lower, item_lower) / 100.0
                s = max(ratio, partial * 0.9)
                if s > best_ratio:
                    best_ratio = s
                    matched = item if s >= DEDUP_THRESHOLD else None

        if matched:
            comments = task.get("comments", "")
            if "From backlog" not in comments:
                task["comments"] = f"From backlog; {comments}" if comments else "From backlog"
            logger.info("Backlog match: '%s' → '%s'", task_name, matched)

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


async def validate_all(
    tasks: list[dict],
    existing_rows: list[dict],
    backlog: list[str],
    sprint_end: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Run all validation rules in order:
      1. Backlog matching (semantic)
      2. Adhoc verification
      3. Dependency normalization
      4. Deduplication (semantic)
      5. Defaults
      6. Schema enforcement

    Returns:
        (new_tasks, update_tasks)
    """
    tasks = await match_backlog(tasks, backlog)
    tasks = verify_adhoc(tasks)
    tasks = normalize_dependencies(tasks)

    new_tasks, update_tasks = await deduplicate(tasks, existing_rows)

    new_tasks = apply_defaults(new_tasks, sprint_end)
    update_tasks = apply_defaults(update_tasks, sprint_end)

    new_tasks = enforce_schema(new_tasks)
    update_tasks = enforce_schema(update_tasks)

    logger.info(
        "Validation complete: %d new, %d updates", len(new_tasks), len(update_tasks)
    )
    return new_tasks, update_tasks
