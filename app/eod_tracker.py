"""
Persistent EOD submission tracker backed by a JSON file.

Stores which members submitted their EOD each day:
  { "2026-05-26": ["Yash", "Aarav"], "2026-05-27": ["Yash"] }
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_FILE = Path("data/eod_submissions.json")
_submissions: dict[str, list[str]] = {}


def load():
    """Load submissions from disk into memory on startup."""
    global _submissions
    if _DATA_FILE.exists():
        try:
            _submissions = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            logger.info("EOD tracker loaded: %d days of data", len(_submissions))
        except Exception as e:
            logger.warning("Failed to load EOD tracker file: %s — starting fresh", e)
            _submissions = {}
    else:
        _submissions = {}


def record(date_str: str, sheet_name: str):
    """Record that a member submitted their EOD on the given date."""
    submitted = _submissions.setdefault(date_str, [])
    if sheet_name not in submitted:
        submitted.append(sheet_name)
        _save()


def get_submitted(date_str: str) -> set[str]:
    """Return the set of members who submitted on the given date."""
    return set(_submissions.get(date_str, []))


def get_month_data(year: int, month: int) -> dict[str, set[str]]:
    """Return all submission data for a given month as { date_str: set of names }."""
    prefix = f"{year:04d}-{month:02d}-"
    return {k: set(v) for k, v in _submissions.items() if k.startswith(prefix)}


def _save():
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        _DATA_FILE.write_text(json.dumps(_submissions, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("Failed to save EOD tracker: %s", e)
