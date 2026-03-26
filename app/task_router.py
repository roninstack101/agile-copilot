"""
Task router — handles backlog-to-sprint promotion.

Runs after validation, before writing to Excel. Matches parsed tasks
against backlog items and marks those for in-place updates on the
existing backlog row instead of appending new rows.
"""

import logging

from fuzzywuzzy import fuzz

from app.config import BACKLOG_PROMOTION_THRESHOLD

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _match_to_backlog(
    name: str, backlog_items: list[dict], threshold: float = BACKLOG_PROMOTION_THRESHOLD
) -> dict | None:
    """
    Fuzzy-match a name against backlog items.
    Returns the best matching backlog item dict or None.
    """
    if not backlog_items or not name:
        return None

    name_lower = name.lower().strip()
    best_match = None
    best_ratio = 0.0

    for item in backlog_items:
        item_text = item["text"].lower().strip()
        ratio = fuzz.partial_ratio(name_lower, item_text) / 100.0
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = item

    if best_match and best_ratio >= threshold:
        return best_match
    return None


# ──────────────────────────────────────────────
# Main routing function
# ──────────────────────────────────────────────


def route_tasks(
    tasks: list[dict],
    backlog_items: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Match tasks against backlog items for in-place promotion.

    Args:
        tasks: validated task dicts from the parser
        backlog_items: from _extract_backlog_with_positions() — [{"text", "row_idx", "col_idx"}]

    Returns:
        (append_tasks, inplace_updates)
        - append_tasks: tasks to append as new rows
        - inplace_updates: tasks to write on existing backlog rows (have _backlog_row_idx)
    """
    if not tasks:
        return [], []

    append_tasks: list[dict] = []
    inplace_updates: list[dict] = []
    promoted_backlogs: set[str] = set()

    for task in tasks:
        task_name = task.get("sprint_backlog", "").strip()

        if task_name and backlog_items:
            matched_backlog = _match_to_backlog(task_name, backlog_items)
            if matched_backlog and matched_backlog["text"].lower() not in promoted_backlogs:
                promoted_backlogs.add(matched_backlog["text"].lower())
                task["_backlog_row_idx"] = matched_backlog["row_idx"]
                task["_backlog_col_idx"] = matched_backlog["col_idx"]
                inplace_updates.append(task)
                logger.info(
                    "Backlog in-place: '%s' matched '%s' (row %d)",
                    task_name, matched_backlog["text"], matched_backlog["row_idx"],
                )
                continue

        append_tasks.append(task)

    logger.info(
        "Routed %d tasks: %d to append, %d backlog in-place updates",
        len(tasks), len(append_tasks), len(inplace_updates),
    )
    return append_tasks, inplace_updates
