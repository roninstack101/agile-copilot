"""
Tests for EOD parsing — local parser and Teams capture utilities.
"""

import json
from pathlib import Path

import pytest

from app.teams_capture import strip_html, validate_eod, extract_metadata, is_eod_message
from app.local_parser import parse_eod_local


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

SAMPLE_PATH = Path(__file__).parent / "sample_eods.json"


@pytest.fixture
def sample_eods():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def default_context():
    return {
        "member_name": "Test User",
        "today_date": "2025-01-15",
        "sprint_end_date": "2025-01-15",
        "backlog_list": [
            "Schneider Catalogue Content changes",
            "Brand Identity Setup Testing",
            "Product listing page updates",
        ],
    }


# ──────────────────────────────────────────────
# HTML stripping tests
# ──────────────────────────────────────────────


class TestStripHtml:
    def test_strips_basic_tags(self):
        html = "<p>Hello <b>world</b></p>"
        result = strip_html(html)
        assert "Hello" in result
        assert "world" in result
        assert "<" not in result

    def test_handles_br_tags(self):
        html = "Line 1<br>Line 2<br/>Line 3"
        result = strip_html(html)
        assert "Line 1" in result
        assert "Line 2" in result

    def test_handles_list_items(self):
        html = "<ul><li>Task 1</li><li>Task 2</li></ul>"
        result = strip_html(html)
        assert "Task 1" in result
        assert "Task 2" in result

    def test_empty_input(self):
        assert strip_html("") == ""
        assert strip_html(None) == ""

    def test_plain_text_passthrough(self):
        text = "No HTML here"
        assert strip_html(text) == text


# ──────────────────────────────────────────────
# EOD validation tests
# ──────────────────────────────────────────────


class TestValidateEod:
    def test_valid_bullet_eod(self):
        text = "Wednesday EOD\n- Task 1\n- Task 2"
        assert validate_eod(text) is True

    def test_valid_asterisk_eod(self):
        text = "EOD\n* Task 1\n* Task 2"
        assert validate_eod(text) is True

    def test_valid_numbered_eod(self):
        text = "EOD\n1. Task 1\n2. Task 2"
        assert validate_eod(text) is True

    def test_invalid_no_bullets(self):
        text = "Hey team, hope everyone had a great day!"
        assert validate_eod(text) is False

    def test_invalid_too_short(self):
        assert validate_eod("Hi") is False

    def test_invalid_empty(self):
        assert validate_eod("") is False


# ──────────────────────────────────────────────
# Metadata extraction tests
# ──────────────────────────────────────────────


class TestExtractMetadata:
    def test_graph_api_format(self):
        payload = {
            "from": {"user": {"displayName": "Aarav Sharma"}},
            "body": {"content": "<p>EOD</p><ul><li>Task 1</li></ul>"},
            "createdDateTime": "2025-01-15T18:30:00Z",
        }
        result = extract_metadata(payload)
        assert result["sender"] == "Aarav Sharma"
        assert "Task 1" in result["clean_message"]
        assert result["timestamp"] == "2025-01-15T18:30:00Z"

    def test_flat_format(self):
        payload = {
            "sender": "Priya Patel",
            "message": "EOD\n- Task 1",
            "timestamp": "2025-01-15T18:30:00Z",
        }
        result = extract_metadata(payload)
        assert result["sender"] == "Priya Patel"
        assert "Task 1" in result["clean_message"]


class TestIsEodMessage:
    def test_with_eod_keyword(self):
        assert is_eod_message("Wednesday EOD\n- task") is True

    def test_without_eod_keyword_but_bullets(self):
        assert is_eod_message("- task 1\n- task 2") is True

    def test_greeting_only(self):
        assert is_eod_message("Good morning team!") is False

    def test_empty(self):
        assert is_eod_message("") is False


# ──────────────────────────────────────────────
# Local parser tests
# ──────────────────────────────────────────────


class TestLocalParser:
    def test_normal_eod(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)
        assert len(tasks) == 7

        # Check task names are extracted
        names = [t["sprint_backlog"] for t in tasks]
        assert any("Schneider" in n for n in names)
        assert any("Banner" in n for n in names)

    def test_adhoc_detection(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        adhoc_tasks = [t for t in tasks if "Adhoc task" in t.get("comments", "")]
        assert len(adhoc_tasks) >= 1

    def test_quantity_marker(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        qty_tasks = [t for t in tasks if "Quantity:" in t.get("comments", "")]
        assert len(qty_tasks) >= 1

    def test_dependency_extraction(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        dep_tasks = [t for t in tasks if t.get("dependency")]
        assert len(dep_tasks) >= 1

    def test_stage_detection(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        stages = [t["stage"] for t in tasks]
        assert "Sent for Approval" in stages
        assert "Closed" in stages
        assert "WIP" in stages

    def test_backlog_matching(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        # Backlog column stays empty, but "From backlog" appears in comments
        from_backlog = [t for t in tasks if "From backlog" in t.get("comments", "")]
        assert len(from_backlog) >= 1

    def test_priority_keywords(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "priority_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        priorities = {t["sprint_backlog"]: t["priority"] for t in tasks}
        # "URGENT" task should be High
        urgent = [p for name, p in priorities.items() if "bug fix" in name.lower()]
        assert urgent and urgent[0] == "High"

    def test_empty_eod(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "empty_eod")
        tasks = parse_eod_local(eod["message"], default_context)
        assert len(tasks) == 0

    def test_complex_eod(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "complex_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        # Should parse all 6 bullet points
        assert len(tasks) == 6

        # Check quantity
        qty_tasks = [t for t in tasks if "Quantity:" in t.get("comments", "")]
        assert len(qty_tasks) >= 1

        # Check completed task
        closed_tasks = [t for t in tasks if t["stage"] == "Closed"]
        assert len(closed_tasks) >= 1

    def test_all_tasks_have_required_fields(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        required_fields = [
            "brand", "activity_type", "backlog", "sprint_backlog",
            "dependency", "deadline", "priority", "stage", "comments",
            "expected_story_points", "actual_story_points",
        ]

        for task in tasks:
            for field in required_fields:
                assert field in task, f"Missing field '{field}' in task: {task}"

    def test_actual_sp_always_zero(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "normal_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        for task in tasks:
            assert task["actual_story_points"] == 0

    def test_story_points_in_range(self, sample_eods, default_context):
        eod = next(e for e in sample_eods if e["id"] == "complex_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        for task in tasks:
            assert 1 <= task["expected_story_points"] <= 13

    # ── Grouped EOD tests (headers are context, not emitted) ──

    def test_grouped_eod_only_bullet_tasks(self, sample_eods, default_context):
        """Grouped EOD should produce only bullet tasks (no section headers)."""
        eod = next(e for e in sample_eods if e["id"] == "grouped_eod")
        tasks = parse_eod_local(eod["message"], default_context)
        # Only bullet tasks, no section headers
        assert len(tasks) == 6
        names = [t["sprint_backlog"].lower() for t in tasks]
        assert "research for parameters" in names
        assert "azure cloud setup" in names

    def test_grouped_eod_stage_detection(self, sample_eods, default_context):
        """Stage keywords should still be detected in grouped bullet tasks."""
        eod = next(e for e in sample_eods if e["id"] == "grouped_eod")
        tasks = parse_eod_local(eod["message"], default_context)

        stages = {t["sprint_backlog"].lower(): t["stage"] for t in tasks}
        assert any(
            "done" in name and stage == "Closed"
            for name, stage in stages.items()
        )

    def test_standalone_bullets_flat(self, default_context):
        """Flat EODs without headers should produce only bullet tasks."""
        eod = "Friday EOD\n- standalone task one\n- standalone task two"
        tasks = parse_eod_local(eod, default_context)
        assert len(tasks) == 2

    def test_mixed_standalone_then_grouped(self, default_context):
        """Standalone bullets followed by a header group — all tasks parsed, no headers emitted."""
        eod = "Monday EOD\n- standalone task\n\nproject alpha:\n- task under alpha\n- another alpha task"
        tasks = parse_eod_local(eod, default_context)
        assert len(tasks) == 3  # 1 standalone + 2 grouped tasks (header skipped)
        assert tasks[0]["sprint_backlog"] == "standalone task"

    def test_activity_type_detection(self, default_context):
        """Tasks with activity-type keywords should get activity_type set."""
        eod = "EOD\n- WEMS website update\n- Reel shoot x1"
        tasks = parse_eod_local(eod, default_context)
        types = {t["sprint_backlog"]: t.get("activity_type", "") for t in tasks}
        assert types.get("WEMS website update") == "Website"
        assert types.get("Reel shoot") == "Social Media"
