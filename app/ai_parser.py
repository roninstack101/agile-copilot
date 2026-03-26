"""
AI parser — calls Gemini (primary) or Groq (fallback) to parse EOD text into structured tasks.
"""

import json
import logging
from pathlib import Path

import httpx

from app.config import settings, get_sprint_end_date, KNOWN_BRANDS, BRAND_PARENT, ACTIVITY_TYPES

logger = logging.getLogger(__name__)

# Load system prompt template
PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.txt"
SYSTEM_PROMPT_TEMPLATE = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else ""

# Task schema for Gemini's structured output (JSON mode)
TASK_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "brand": {"type": "string"},
            "activity_type": {"type": "string"},
            "backlog": {"type": "string"},
            "sprint_backlog": {"type": "string"},
            "dependency": {"type": "string"},
            "deadline": {"type": "string"},
            "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
            "stage": {"type": "string", "enum": ["WIP", "Sent for Approval", "Closed"]},
            "comments": {"type": "string"},
            "expected_story_points": {"type": "integer"},
            "actual_story_points": {"type": "integer"},
        },
        "required": [
            "brand",
            "activity_type",
            "backlog",
            "sprint_backlog",
            "dependency",
            "deadline",
            "priority",
            "stage",
            "comments",
            "expected_story_points",
            "actual_story_points",
        ],
    },
}


def _remap_sub_brands(tasks: list[dict]) -> list[dict]:
    """Remap sub-brand names to their parent brand."""
    for task in tasks:
        brand = task.get("brand", "")
        if brand in BRAND_PARENT:
            task["brand"] = BRAND_PARENT[brand]
    return tasks


def _postprocess(tasks: list[dict]) -> list[dict]:
    """Apply all post-processing steps to parsed tasks."""
    tasks = _remap_sub_brands(tasks)
    # Clear AI-set backlog field — backlog matching is handled by the validator
    for task in tasks:
        task["backlog"] = ""
        # Ensure activity_type field exists
        task.setdefault("activity_type", "")
    return tasks


def _build_existing_tasks_summary(existing_rows: list[dict]) -> str:
    """
    Build a brand-grouped tasks summary so the AI can learn patterns like:
      WOGOM (Social Media): Reel shoot [WIP], Micro Fiction [Closed]
      WOGOM (Content): Podcast draft [WIP]
    """
    if not existing_rows:
        return "  (no existing tasks)"

    # Group by (brand, activity_type) for pattern clarity
    groups: dict[tuple[str, str], list[str]] = {}
    for row in existing_rows:
        name = row.get("sprint_backlog", "")
        if not name:
            continue
        brand = row.get("brand", "") or "Unknown"
        activity = row.get("activity_type", "") or "Unknown"
        stage = row.get("stage", "WIP")
        groups.setdefault((brand, activity), []).append(f"{name} [{stage}]")

    lines = []
    for (brand, activity), tasks in sorted(groups.items()):
        # Show at most 8 tasks per group
        display = tasks[:8]
        task_list = ", ".join(display)
        if len(tasks) > 8:
            task_list += f", ... (+{len(tasks) - 8} more)"
        lines.append(f"  {brand} ({activity}): {task_list}")

    # Cap total lines
    if len(lines) > 25:
        lines = lines[:25] + [f"  ... (+{len(lines) - 25} more groups)"]

    return "\n".join(lines) if lines else "  (no existing tasks)"


def _build_prompt(eod_text: str, context: dict) -> str:
    """Build the full prompt by injecting dynamic context into the template."""
    member_name = context.get("member_name", "Unknown")
    today = context.get("today_date", "")
    sprint_end = context.get("sprint_end_date", get_sprint_end_date())
    backlog_list = context.get("backlog_list", [])
    brand_list = context.get("brand_list", KNOWN_BRANDS)
    activity_types = context.get("activity_types", ACTIVITY_TYPES)
    existing_rows = context.get("existing_rows", [])

    backlog_str = "\n".join(f"  - {item}" for item in backlog_list) if backlog_list else "  (no backlog items)"
    brand_str = "\n".join(f"  - {brand}" for brand in brand_list) if brand_list else "  (no brands)"
    activity_str = "\n".join(f"  - {at}" for at in activity_types) if activity_types else "  (no activity types)"
    tasks_str = _build_existing_tasks_summary(existing_rows)

    prompt = SYSTEM_PROMPT_TEMPLATE
    prompt = prompt.replace("{{MEMBER_NAME}}", member_name)
    prompt = prompt.replace("{{TODAY_DATE}}", today)
    prompt = prompt.replace("{{SPRINT_END_DATE}}", sprint_end)
    prompt = prompt.replace("{{BACKLOG_LIST}}", backlog_str)
    prompt = prompt.replace("{{BRAND_LIST}}", brand_str)
    prompt = prompt.replace("{{ACTIVITY_TYPE_LIST}}", activity_str)
    prompt = prompt.replace("{{EXISTING_TASKS}}", tasks_str)

    return prompt + f"\n\n---\nEOD MESSAGE:\n{eod_text}"


async def parse_with_gemini(eod_text: str, context: dict) -> list[dict]:
    """
    Parse EOD using Google Gemini 1.5 Flash with JSON mode.
    Returns a list of task dictionaries.
    """
    full_prompt = _build_prompt(eod_text, context)

    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": TASK_SCHEMA,
        },
    }

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={settings.GEMINI_API_KEY}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    # Extract the generated text from Gemini's response
    candidates = data.get("candidates", [])
    if not candidates:
        logger.warning("Gemini returned no candidates")
        return []

    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "[]")
    tasks = json.loads(text)

    logger.info("Gemini parsed %d tasks", len(tasks))
    return _postprocess(tasks) if isinstance(tasks, list) else []


async def parse_with_groq(eod_text: str, context: dict) -> list[dict]:
    """
    Parse EOD using Groq (Llama 3.1 70B) as fallback.
    Returns a list of task dictionaries.
    """
    full_prompt = _build_prompt(eod_text, context)

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": full_prompt},
            {"role": "user", "content": eod_text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }

    headers = {
        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)

    # Groq might return {"tasks": [...]} or just [...]
    if isinstance(parsed, dict):
        tasks = parsed.get("tasks", parsed.get("data", []))
    elif isinstance(parsed, list):
        tasks = parsed
    else:
        tasks = []

    logger.info("Groq parsed %d tasks", len(tasks))
    return _postprocess(tasks)


def _enrich_with_ai(local_tasks: list[dict], ai_tasks: list[dict]) -> list[dict]:
    """
    Hybrid approach: keep the local parser's structure (headers, grouping)
    but enrich individual tasks with AI's field inference (brand, stage,
    priority, story points, comments).

    The local parser is the source of truth for structure; AI is the source
    of truth for field values on matching tasks.
    """
    from fuzzywuzzy import fuzz

    enriched = []
    for task in local_tasks:
        task_name = task.get("sprint_backlog", "").lower()

        # Find best matching AI task
        best_match = None
        best_ratio = 0.0
        for ai_task in ai_tasks:
            ai_name = ai_task.get("sprint_backlog", "").lower()
            ratio = fuzz.ratio(task_name, ai_name) / 100.0
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = ai_task

        if best_match and best_ratio >= 0.6:
            # Merge: AI fields override local, but keep local's sprint_backlog
            ENRICH_FIELDS = ["brand", "activity_type", "stage", "priority",
                             "comments", "expected_story_points", "dependency"]
            for field in ENRICH_FIELDS:
                ai_val = best_match.get(field)
                if ai_val and str(ai_val).strip() and str(ai_val) != "0":
                    # For stage: if local parser detected "Sent for Approval"
                    # (explicit review/approval keywords), don't let AI
                    # override to "Closed" — the approval signal is more specific
                    if field == "stage":
                        local_stage = task.get("stage", "WIP")
                        if local_stage == "Sent for Approval" and ai_val == "Closed":
                            continue
                    task[field] = ai_val
            logger.info(
                "Enriched '%s' with AI (brand='%s', activity='%s', stage='%s')",
                task["sprint_backlog"],
                task.get("brand", ""),
                task.get("activity_type", ""),
                task.get("stage", ""),
            )

        enriched.append(task)

    return enriched


async def parse_eod(eod_text: str, context: dict) -> list[dict]:
    """
    Hybrid parsing: local parser for structure + AI for enrichment.

    1. Local parser extracts structure (headers, bullets, grouping) — always reliable
    2. AI (Gemini → Groq) enriches fields (brand, stage, priority, story points)
    3. If AI fails entirely, local parser output is used as-is

    Returns a list of task dictionaries.
    """
    from app.local_parser import parse_eod_local

    # Step 1: Local parser — always runs, provides reliable structure
    local_tasks = parse_eod_local(eod_text, context)
    if not local_tasks:
        return []

    logger.info("Local parser extracted %d tasks (structure source)", len(local_tasks))

    # Step 2: Try AI for enrichment (Gemini → Groq)
    ai_tasks = None

    if settings.GEMINI_API_KEY:
        try:
            ai_tasks = await parse_with_gemini(eod_text, context)
            if ai_tasks:
                logger.info("Gemini returned %d tasks for enrichment", len(ai_tasks))
            else:
                logger.warning("Gemini returned empty, trying Groq")
                ai_tasks = None
        except Exception as e:
            logger.warning("Gemini failed: %s, trying Groq", e)

    if not ai_tasks and settings.GROQ_API_KEY:
        try:
            ai_tasks = await parse_with_groq(eod_text, context)
            if ai_tasks:
                logger.info("Groq returned %d tasks for enrichment", len(ai_tasks))
            else:
                logger.warning("Groq returned empty")
                ai_tasks = None
        except Exception as e:
            logger.warning("Groq failed: %s", e)

    # Step 3: Merge — local structure + AI enrichment
    if ai_tasks:
        result = _enrich_with_ai(local_tasks, _postprocess(ai_tasks))
        logger.info("Hybrid parse: %d tasks (local structure + AI enrichment)", len(result))
        return result

    # AI unavailable — local parser output is still solid
    logger.info("Using local parser only (AI unavailable): %d tasks", len(local_tasks))
    return local_tasks
