"""
Local regex-based EOD parser — last-resort fallback when both LLMs fail.
Handles the 80% case using string manipulation and keyword matching.
"""

import re
import logging
from app.config import get_sprint_end_date, KNOWN_BRANDS, BRAND_PARENT, ACTIVITY_TYPES

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Keyword sets for classification
# ──────────────────────────────────────────────

HIGH_PRIORITY_KEYWORDS = {
    "urgent", "critical", "asap", "blocker", "p0", "high priority", "priority: high",
}
LOW_PRIORITY_KEYWORDS = {
    "minor", "low", "nice to have", "low priority", "priority: low",
}

APPROVAL_KEYWORDS = {
    "submitted", "sent for review", "shared with manager", "under review",
    "sent for approval", "awaiting approval", "review pending",
    "pending review", "review from", "approval pending", "pending approval",
}
CLOSED_KEYWORDS = {
    "done", "completed", "finished", "delivered", "closed", "shipped",
    "finalized", "approved", "published", "uploaded",
    "handed over", "handover",
}

DEPENDENCY_KEYWORDS = {
    "waiting", "blocked", "needs", "dependent on", "dependency",
}

# Brand aliases: map alternate names to canonical brand name
BRAND_ALIASES = {
    "narnarayan": "Nar Narayan",
    "nar narayan": "Nar Narayan",
}

# Activity type keyword mapping
ACTIVITY_TYPE_KEYWORDS = {
    "Social Media": {"reel", "post", "story", "stories", "social media", "instagram", "facebook", "linkedin", "micro fiction", "microfiction"},
    "Collateral": {"banner", "brochure", "flyer", "collateral", "poster", "pamphlet", "leaflet"},
    "Website": {"website", "web", "landing page", "homepage", "figma", "ui", "ux", "wireframe", "sitemap"},
    "Branding": {"brand", "branding", "logo", "identity", "brand identity", "brand guideline"},
    "Ops": {"ops", "operations", "admin", "setup", "cloud", "server", "deploy", "infrastructure", "azure"},
    "Content": {"content", "blog", "article", "copy", "copywriting", "catalogue", "catalog", "draft", "script", "podcast"},
    "Digital Marketing": {"seo", "ads", "campaign", "digital marketing", "ppc", "google ads", "meta ads", "marketing"},
}


def _detect_brand(line: str, brand_list: list[str] | None = None) -> str:
    """Detect brand/project from task text. Sub-brands resolve to their parent."""
    lower = line.lower()
    brands = brand_list or KNOWN_BRANDS

    # Check aliases first
    for alias, canonical in BRAND_ALIASES.items():
        if alias in lower:
            return BRAND_PARENT.get(canonical, canonical)

    # Check known brands
    for brand in brands:
        if brand.lower() in lower:
            return BRAND_PARENT.get(brand, brand)

    return ""


def _detect_activity_type(line: str) -> str:
    """Detect activity type from keywords in the line."""
    lower = line.lower()
    for activity_type, keywords in ACTIVITY_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return activity_type
    return ""


def _detect_priority(line: str) -> str:
    """Detect priority from keywords in the line."""
    lower = line.lower()
    for kw in HIGH_PRIORITY_KEYWORDS:
        if kw in lower:
            return "High"
    for kw in LOW_PRIORITY_KEYWORDS:
        if kw in lower:
            return "Low"
    return "Medium"


def _detect_stage(line: str) -> str:
    """Detect task stage from keywords in the line.

    Check approval FIRST — 'review pending' beats 'done' because
    the task isn't truly closed if someone still needs to review it.
    """
    lower = line.lower()
    for kw in APPROVAL_KEYWORDS:
        if kw in lower:
            return "Sent for Approval"
    for kw in CLOSED_KEYWORDS:
        if kw in lower:
            return "Closed"
    return "WIP"


def _extract_dependency(line: str) -> tuple[str, str]:
    """
    Extract dependency text from parenthetical remarks.
    Returns (cleaned_line, dependency_text).
    """
    dep_pattern = re.compile(
        r"\((?:dependency:\s*|dep:\s*)?(.*?(?:"
        + "|".join(DEPENDENCY_KEYWORDS)
        + r").*?)\)",
        re.IGNORECASE,
    )
    match = dep_pattern.search(line)
    if match:
        dep_text = match.group(1).strip()
        # Normalize: capitalize first letter, clean up
        dep_text = dep_text[0].upper() + dep_text[1:] if dep_text else ""
        cleaned_line = line[: match.start()] + line[match.end() :]
        return cleaned_line.strip(), dep_text
    return line, ""


def _extract_quantity(line: str) -> tuple[str, int]:
    """
    Extract quantity marker (x2, x3, etc.) from end of line.
    Returns (cleaned_line, quantity).
    """
    qty_pattern = re.compile(r"\s+x(\d+)\s*$", re.IGNORECASE)
    match = qty_pattern.search(line)
    if match:
        qty = int(match.group(1))
        cleaned_line = line[: match.start()].strip()
        return cleaned_line, qty
    return line, 1


def _extract_adhoc(line: str) -> tuple[str, bool]:
    """
    Detect and strip 'Adhoc:' or 'Ad-hoc:' prefix.
    Returns (cleaned_line, is_adhoc).
    """
    adhoc_pattern = re.compile(r"^\s*(?:adhoc|ad-hoc)\s*:\s*", re.IGNORECASE)
    match = adhoc_pattern.match(line)
    if match:
        return line[match.end() :].strip(), True
    return line, False


def _estimate_story_points(task_name: str, quantity: int) -> int:
    """
    Estimate story points based on effort:
      1 = half day (quick fix, minor change, single draft)
      2 = 1 day   (standard single-deliverable task)
      5 = 2 days  (multi-part work, setup, research)
      8 = 3 days  (large feature, integration, redesign)
    Multiply by quantity, clamp to 1–13.
    """
    lower = task_name.lower()

    if any(kw in lower for kw in ["fix", "typo", "tweak", "minor", "change", "update"]):
        base = 1  # half day
    elif any(kw in lower for kw in ["setup", "testing", "test", "research", "explore",
                                     "migration", "multi", "architecture"]):
        base = 5  # 2 days
    elif any(kw in lower for kw in ["feature", "integration", "redesign", "full",
                                     "complete", "overhaul"]):
        base = 8  # 3 days
    else:
        base = 2  # 1 day (standard task)

    total = base * quantity
    return max(1, min(13, total))


def _fuzzy_match_backlog(task_name: str, backlog_list: list[str]) -> str | None:
    """
    Smart fuzzy match against backlog items.
    Uses substring matching + word overlap to catch cases like:
      - "WEMS Catalogue draft" matching backlog "Catalogue" (under WEMS brand)
      - "Brand Identity Setup Testing" matching "Brand Identity Setup"
    """
    if not backlog_list:
        return None

    task_lower = task_name.lower().strip()
    task_words = set(task_lower.split())
    # Remove very common filler words from comparison
    filler = {"the", "a", "an", "of", "for", "and", "in", "on", "to", "x1", "x2", "x3"}
    task_words -= filler

    best_match = None
    best_score = 0.0

    for item in backlog_list:
        item_lower = item.lower().strip()
        item_words = set(item_lower.split()) - filler

        # Method 1: Substring containment (either direction)
        if task_lower in item_lower or item_lower in task_lower:
            # Guard: backlog item must be at least 40% of task name length
            if len(item_lower) >= len(task_lower) * 0.4:
                score = 0.95
            else:
                score = 0.5
        # Method 2: Word overlap — what fraction of backlog words appear in task?
        elif item_words and task_words:
            overlap = len(item_words & task_words)
            score = overlap / len(item_words)  # % of backlog words matched
        else:
            score = 0.0

        if score > best_score:
            best_score = score
            best_match = item

    # Require at least 70% word overlap or strong substring match
    if best_match and best_score >= 0.7:
        return best_match
    return None


def _is_bullet_line(text: str) -> bool:
    """Check if a line starts with a bullet or number prefix."""
    return bool(re.match(r"^\s*[-•*]\s*(.+)|^\s*\d+[.)]\s*(.+)", text))


def _smart_title(text: str) -> str:
    """Title-case only if text is all-lowercase; otherwise preserve original casing."""
    if text == text.lower():
        return text.title()
    return text


def parse_eod_local(eod_text: str, context: dict) -> list[dict]:
    """
    Parse an EOD message using regex / keyword rules. No AI needed.

    Supports two EOD formats:
      1. Flat bullets (existing): each bullet is an independent task
      2. Grouped (new): non-bullet header lines act as section labels in Sprint Backlog,
         with bullet sub-tasks nested underneath

    Args:
        eod_text: cleaned plain-text EOD message
        context: dict with member_name, sprint_end_date, backlog_list, today_date

    Returns:
        list of task dictionaries matching the agile sheet schema
    """
    sprint_end = context.get("sprint_end_date", get_sprint_end_date())
    backlog_list = context.get("backlog_list", [])

    lines = eod_text.strip().split("\n")
    tasks = []

    # Check upfront if EOD has any bullets at all
    has_any_bullets = any(_is_bullet_line(l) for l in lines if l.strip())

    # Smart preamble skip: skip name/greeting/eod lines but stop at content
    start_idx = 0
    for i, line in enumerate(lines):
        sl = line.strip().lower()
        if not sl:
            start_idx = i + 1
            continue
        # Skip lines containing "eod" or starting with day names
        if "eod" in sl or re.match(r"^(mon|tue|wed|thu|fri|sat|sun)", sl, re.IGNORECASE):
            start_idx = i + 1
            continue
        # If this line is a bullet, content starts here
        if _is_bullet_line(line):
            break
        # No bullets in EOD → first non-preamble line is content, stop skipping
        if not has_any_bullets:
            break
        # If a bullet follows (possibly after blank lines), this is a header — stop
        has_bullet_ahead = False
        for j in range(i + 1, min(i + 4, len(lines))):
            if lines[j].strip() and _is_bullet_line(lines[j]):
                has_bullet_ahead = True
                break
            if lines[j].strip() and not _is_bullet_line(lines[j]):
                break  # non-empty non-bullet line — not a header
        if has_bullet_ahead:
            break
        # Otherwise it's preamble (name, greeting, etc.) — skip
        start_idx = i + 1

    # Main parsing loop with section header tracking
    current_header = ""

    for line in lines[start_idx:]:
        stripped = line.strip()
        if not stripped:
            continue

        # Check for bullet or numbered line
        bullet_match = re.match(r"^[-•*]\s*(.+)", stripped)
        numbered_match = re.match(r"^\d+[.)]\s*(.+)", stripped)

        if bullet_match:
            task_text = bullet_match.group(1).strip()
        elif numbered_match:
            task_text = numbered_match.group(1).strip()
        elif not has_any_bullets:
            # No bullets in entire EOD → treat each line as a task
            task_text = stripped
        else:
            # Has bullets elsewhere → non-bullet line is a context header
            header_text = stripped.rstrip(":").strip()
            if header_text:
                current_header = _smart_title(header_text)
            continue

        # ── Process bullet task ──

        # Extract components
        task_text, is_adhoc = _extract_adhoc(task_text)
        task_text, dependency = _extract_dependency(task_text)
        task_text, quantity = _extract_quantity(task_text)

        # Detect brand — check task text first, then fall back to header context
        brand = _detect_brand(task_text)
        if not brand and current_header:
            brand = _detect_brand(current_header)

        # Detect activity type
        activity_type = _detect_activity_type(task_text)

        # Detect priority and stage
        priority = _detect_priority(task_text)
        stage = _detect_stage(task_text)

        # Clean task name: remove trailing stage keywords (e.g. "— done")
        clean_name = re.sub(
            r"\s*[—–-]\s*(done|completed|finished|delivered|submitted|sent for review|"
            r"shared with manager|under review|closed|shipped)\s*$",
            "",
            task_text,
            flags=re.IGNORECASE,
        ).strip()

        # Backlog matching
        backlog_match = _fuzzy_match_backlog(clean_name, backlog_list)

        # Build comments
        comments_parts = []
        if backlog_match:
            comments_parts.append("From backlog")
        if is_adhoc and not backlog_match:
            comments_parts.append("Adhoc task")
        if quantity > 1:
            comments_parts.append(f"Quantity: {quantity}")

        # Estimate story points
        sp = _estimate_story_points(clean_name, quantity)

        task = {
            "brand": brand,
            "activity_type": activity_type,
            "backlog": "",
            "sprint_backlog": clean_name,
            "dependency": dependency,
            "deadline": sprint_end,
            "priority": priority,
            "stage": stage,
            "comments": "; ".join(comments_parts),
            "expected_story_points": sp,
            "actual_story_points": 0,
        }
        tasks.append(task)

    logger.info("Local parser extracted %d tasks", len(tasks))
    return tasks
