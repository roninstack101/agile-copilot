"""
Tests for the validation layer — dedup, backlog matching, defaults, schema enforcement.
"""

import pytest

from app.validator import (
    deduplicate,
    match_backlog,
    apply_defaults,
    verify_adhoc,
    normalize_dependencies,
    enforce_schema,
    validate_all,
)
from app.config import (
    ALLOWED_PRIORITIES,
    ALLOWED_STAGES,
    DEFAULT_PRIORITY,
    DEFAULT_STAGE,
    MIN_SP,
    MAX_SP,
)


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def sample_tasks():
    return [
        {
            "brand": "",
            "activity_type": "",
            "backlog": "",
            "sprint_backlog": "Dashboard wireframe redesign",
            "dependency": "",
            "deadline": "2025-01-15",
            "priority": "Medium",
            "stage": "WIP",
            "comments": "",
            "expected_story_points": 3,
            "actual_story_points": 0,
        },
        {
            "brand": "",
            "activity_type": "",
            "backlog": "",
            "sprint_backlog": "API integration testing",
            "dependency": "Backend team – API endpoints",
            "deadline": "2025-01-15",
            "priority": "High",
            "stage": "WIP",
            "comments": "",
            "expected_story_points": 5,
            "actual_story_points": 0,
        },
        {
            "brand": "",
            "activity_type": "",
            "backlog": "",
            "sprint_backlog": "Homepage slider fix",
            "dependency": "",
            "deadline": "2025-01-15",
            "priority": "Medium",
            "stage": "Closed",
            "comments": "Adhoc task",
            "expected_story_points": 1,
            "actual_story_points": 0,
        },
    ]


@pytest.fixture
def existing_rows():
    return [
        {
            "backlog": "Dashboard wireframe",
            "sprint_backlog": "Dashboard wireframe redesign",
            "dependency": "",
            "deadline": "2025-01-15",
            "priority": "Medium",
            "stage": "WIP",
            "comments": "From backlog",
            "expected_story_points": 3,
            "actual_story_points": 0,
        },
    ]


@pytest.fixture
def backlog_list():
    return [
        "Dashboard wireframe",
        "API integration",
        "Product listing page",
        "Email template design",
    ]


# ──────────────────────────────────────────────
# Deduplication tests
# ──────────────────────────────────────────────


class TestDeduplicate:
    def test_detects_duplicate(self, sample_tasks, existing_rows):
        new_tasks, updates = deduplicate(sample_tasks, existing_rows)
        # "Dashboard wireframe redesign" should match existing "Dashboard wireframe redesign"
        assert len(updates) == 1
        assert len(new_tasks) == 2

    def test_no_duplicates(self, sample_tasks):
        new_tasks, updates = deduplicate(sample_tasks, [])
        assert len(new_tasks) == 3
        assert len(updates) == 0

    def test_update_preserves_row_index(self, sample_tasks, existing_rows):
        _, updates = deduplicate(sample_tasks, existing_rows)
        assert updates[0].get("_row_index") == 0


# ──────────────────────────────────────────────
# Backlog matching tests
# ──────────────────────────────────────────────


class TestMatchBacklog:
    def test_matches_backlog_item(self, sample_tasks, backlog_list):
        result = match_backlog(sample_tasks, backlog_list)
        # "Dashboard wireframe redesign" should match "Dashboard wireframe"
        dashboard_task = next(t for t in result if "Dashboard" in t["sprint_backlog"])
        # Backlog column stays empty — item already exists in its own row
        assert dashboard_task["backlog"] == ""
        assert "From backlog" in dashboard_task["comments"]

    def test_no_match(self, backlog_list):
        tasks = [{"sprint_backlog": "Totally unrelated task", "backlog": "", "comments": ""}]
        result = match_backlog(tasks, backlog_list)
        assert result[0]["backlog"] == ""

    def test_empty_backlog(self, sample_tasks):
        result = match_backlog(sample_tasks, [])
        for task in result:
            assert task["backlog"] == ""


# ──────────────────────────────────────────────
# Defaults tests
# ──────────────────────────────────────────────


class TestApplyDefaults:
    def test_valid_priority_unchanged(self):
        tasks = [{"priority": "High", "stage": "WIP", "deadline": "2025-01-15",
                   "expected_story_points": 3, "actual_story_points": 0,
                   "sprint_backlog": "Task"}]
        result = apply_defaults(tasks)
        assert result[0]["priority"] == "High"

    def test_invalid_priority_defaulted(self):
        tasks = [{"priority": "INVALID", "stage": "WIP", "deadline": "2025-01-15",
                   "expected_story_points": 3, "actual_story_points": 0,
                   "sprint_backlog": "Task"}]
        result = apply_defaults(tasks)
        assert result[0]["priority"] == DEFAULT_PRIORITY

    def test_story_points_clamped(self):
        tasks = [{"expected_story_points": 50, "actual_story_points": 5,
                   "priority": "Medium", "stage": "WIP", "deadline": "2025-01-15",
                   "sprint_backlog": "Task"}]
        result = apply_defaults(tasks)
        assert result[0]["expected_story_points"] == MAX_SP
        assert result[0]["actual_story_points"] == 0  # always 0 on creation

    def test_empty_sprint_backlog_gets_default(self):
        tasks = [{"sprint_backlog": "", "priority": "Medium", "stage": "WIP",
                   "deadline": "2025-01-15", "expected_story_points": 2,
                   "actual_story_points": 0}]
        result = apply_defaults(tasks)
        assert result[0]["sprint_backlog"] == "Untitled task"

    def test_invalid_date_defaulted(self):
        tasks = [{"deadline": "not-a-date", "priority": "Medium", "stage": "WIP",
                   "expected_story_points": 2, "actual_story_points": 0,
                   "sprint_backlog": "Task"}]
        result = apply_defaults(tasks, sprint_end="2025-01-31")
        assert result[0]["deadline"] == "2025-01-31"


# ──────────────────────────────────────────────
# Adhoc verification tests
# ──────────────────────────────────────────────


class TestVerifyAdhoc:
    def test_adhoc_with_backlog_match_removed(self):
        tasks = [{"comments": "Adhoc task", "backlog": "Dashboard wireframe"}]
        result = verify_adhoc(tasks)
        assert "Adhoc task" not in result[0]["comments"]
        assert "From backlog" in result[0]["comments"]

    def test_adhoc_without_backlog_unchanged(self):
        tasks = [{"comments": "Adhoc task", "backlog": ""}]
        result = verify_adhoc(tasks)
        assert "Adhoc task" in result[0]["comments"]


# ──────────────────────────────────────────────
# Dependency normalization tests
# ──────────────────────────────────────────────


class TestNormalizeDependencies:
    def test_normalizes_format(self):
        tasks = [{"dependency": "backend team - api endpoints"}]
        result = normalize_dependencies(tasks)
        assert result[0]["dependency"] == "Backend Team – api endpoints"

    def test_empty_dependency_unchanged(self):
        tasks = [{"dependency": ""}]
        result = normalize_dependencies(tasks)
        assert result[0]["dependency"] == ""


# ──────────────────────────────────────────────
# Schema enforcement tests
# ──────────────────────────────────────────────


class TestEnforceSchema:
    def test_wip_stage(self):
        tasks = [{"brand": "", "activity_type": "", "stage": "WIP", "backlog": "",
                   "sprint_backlog": "Task", "dependency": "", "deadline": "2025-01-15",
                   "priority": "Medium", "comments": "", "expected_story_points": 2,
                   "actual_story_points": 0}]
        result = enforce_schema(tasks)
        assert result[0]["stage"] == "WIP"

    def test_closed_stage(self):
        tasks = [{"brand": "", "activity_type": "", "stage": "Closed", "backlog": "",
                   "sprint_backlog": "Task", "dependency": "", "deadline": "2025-01-15",
                   "priority": "Medium", "comments": "", "expected_story_points": 2,
                   "actual_story_points": 0}]
        result = enforce_schema(tasks)
        assert result[0]["stage"] == "Closed"

    def test_field_length_truncation(self):
        tasks = [{"sprint_backlog": "A" * 1000, "stage": "WIP", "brand": "",
                   "activity_type": "", "backlog": "", "dependency": "", "deadline": "",
                   "priority": "", "comments": "", "expected_story_points": 2,
                   "actual_story_points": 0}]
        result = enforce_schema(tasks)
        assert len(result[0]["sprint_backlog"]) == 500

    def test_invalid_activity_type_cleared(self):
        tasks = [{"brand": "", "activity_type": "Invalid Type", "stage": "WIP",
                   "backlog": "", "sprint_backlog": "Task", "dependency": "",
                   "deadline": "2025-01-15", "priority": "Medium", "comments": "",
                   "expected_story_points": 2, "actual_story_points": 0}]
        result = enforce_schema(tasks)
        assert result[0]["activity_type"] == ""

    def test_valid_activity_type_preserved(self):
        tasks = [{"brand": "", "activity_type": "Website", "stage": "WIP",
                   "backlog": "", "sprint_backlog": "Task", "dependency": "",
                   "deadline": "2025-01-15", "priority": "Medium", "comments": "",
                   "expected_story_points": 2, "actual_story_points": 0}]
        result = enforce_schema(tasks)
        assert result[0]["activity_type"] == "Website"


# ──────────────────────────────────────────────
# Row conversion tests
# ──────────────────────────────────────────────


class TestBrandPreservation:
    def test_brand_preserved_in_schema(self):
        tasks = [{
            "brand": "WEMS", "backlog": "", "sprint_backlog": "WEMS website",
            "dependency": "", "deadline": "2025-01-15",
            "priority": "Medium", "stage": "WIP",
            "comments": "", "expected_story_points": 3, "actual_story_points": 0,
        }]
        result = enforce_schema(tasks)
        assert result[0]["brand"] == "WEMS"
        assert result[0]["sprint_backlog"] == "WEMS website"


# ──────────────────────────────────────────────
# Full pipeline tests
# ──────────────────────────────────────────────


class TestValidateAll:
    def test_full_pipeline(self, sample_tasks, existing_rows, backlog_list):
        new_tasks, update_tasks = validate_all(
            sample_tasks, existing_rows, backlog_list, "2025-01-15"
        )

        # Should have some new tasks and possibly some updates
        assert isinstance(new_tasks, list)
        assert isinstance(update_tasks, list)

        # Each new task should be a dict with required fields
        for task in new_tasks:
            assert isinstance(task, dict)
            assert "sprint_backlog" in task
            assert "stage" in task

    def test_empty_input(self):
        new_tasks, update_tasks = validate_all([], [], [], "2025-01-15")
        assert len(new_tasks) == 0
        assert len(update_tasks) == 0
