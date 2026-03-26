"""
Tests for task_router — backlog promotion (in-place updates).
"""

import pytest

from app.task_router import route_tasks, _match_to_backlog


# ──────────────────────────────────────────────
# Helper factories
# ──────────────────────────────────────────────


def _task(name: str, brand: str = "", activity_type: str = "", stage: str = "WIP", **kwargs) -> dict:
    """Create a minimal task dict."""
    return {
        "brand": brand,
        "activity_type": activity_type,
        "backlog": "",
        "sprint_backlog": name,
        "dependency": "",
        "deadline": "2025-01-15",
        "priority": "Medium",
        "stage": stage,
        "comments": "",
        "expected_story_points": 2,
        "actual_story_points": 0,
        **kwargs,
    }


def _backlog_item(text: str, row_idx: int = 5, col_idx: int = 0) -> dict:
    """Create a backlog item with position."""
    return {"text": text, "row_idx": row_idx, "col_idx": col_idx}


# ──────────────────────────────────────────────
# Unit tests — _match_to_backlog
# ──────────────────────────────────────────────


class TestMatchToBacklog:
    def test_exact_match(self):
        items = [_backlog_item("Brand Identity Setup")]
        result = _match_to_backlog("Brand Identity Setup", items)
        assert result is not None
        assert result["text"] == "Brand Identity Setup"

    def test_fuzzy_match(self):
        items = [_backlog_item("Brand Identity Setup Testing")]
        result = _match_to_backlog("Brand Identity Setup", items)
        assert result is not None

    def test_no_match(self):
        items = [_backlog_item("Schneider Catalogue Content")]
        result = _match_to_backlog("Azure Cloud Setup", items)
        assert result is None

    def test_empty_backlog(self):
        result = _match_to_backlog("anything", [])
        assert result is None


# ──────────────────────────────────────────────
# Integration tests — route_tasks()
# ──────────────────────────────────────────────


class TestRouteTasksBacklogPromotion:
    def test_task_matches_backlog_inplace(self):
        """Task matching a backlog item → in-place update on backlog row."""
        tasks = [_task("WEMS Catalogue draft", brand="WEMS")]
        backlog = [_backlog_item("WEMS Catalogue draft", row_idx=15, col_idx=0)]

        append, inplace = route_tasks(tasks, backlog)

        # Task should NOT be in append — it's an in-place update
        assert len(append) == 0
        assert len(inplace) == 1
        assert inplace[0]["_backlog_row_idx"] == 15
        assert inplace[0]["sprint_backlog"] == "WEMS Catalogue draft"

    def test_no_backlog_match_appends(self):
        """Task with no backlog match → appended as new row."""
        tasks = [_task("New task", brand="WEMS")]
        backlog = [_backlog_item("Unrelated backlog item", row_idx=10)]

        append, inplace = route_tasks(tasks, backlog)

        assert len(append) == 1
        assert len(inplace) == 0
        assert append[0]["sprint_backlog"] == "New task"

    def test_empty_backlog_all_append(self):
        """No backlog items → all tasks appended."""
        tasks = [_task("Task one"), _task("Task two")]

        append, inplace = route_tasks(tasks, [])

        assert len(append) == 2
        assert len(inplace) == 0

    def test_multiple_tasks_mixed(self):
        """Mix of backlog matches and new tasks."""
        tasks = [
            _task("Brand Identity Setup"),
            _task("New feature work"),
            _task("Catalogue update", brand="WEMS"),
        ]
        backlog = [
            _backlog_item("Brand Identity Setup", row_idx=5),
            _backlog_item("Catalogue update", row_idx=12),
        ]

        append, inplace = route_tasks(tasks, backlog)

        assert len(append) == 1  # "New feature work"
        assert len(inplace) == 2  # Both backlog matches
        assert append[0]["sprint_backlog"] == "New feature work"

    def test_same_backlog_not_promoted_twice(self):
        """Same backlog item can only be promoted once."""
        tasks = [
            _task("Catalogue update"),
            _task("Catalogue update v2"),  # similar but different
        ]
        backlog = [_backlog_item("Catalogue update", row_idx=10)]

        append, inplace = route_tasks(tasks, backlog)

        # Only first match gets promoted
        assert len(inplace) == 1
        assert len(append) == 1

    def test_empty_tasks(self):
        """Empty task list returns empty."""
        append, inplace = route_tasks([], [])
        assert append == []
        assert inplace == []

    def test_preserves_brand_and_activity_type(self):
        """Brand and activity_type are preserved through routing."""
        tasks = [_task("Website update", brand="WEMS", activity_type="Website")]

        append, inplace = route_tasks(tasks, [])

        assert len(append) == 1
        assert append[0]["brand"] == "WEMS"
        assert append[0]["activity_type"] == "Website"
