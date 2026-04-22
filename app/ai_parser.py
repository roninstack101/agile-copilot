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
        display = tasks[:8]
        task_list = ", ".join(display)
        if len(tasks) > 8:
            task_list += f", ... (+{len(tasks) - 8} more)"
        lines.append(f"  {brand} ({activity}): {task_list}")

    return "\n".join(lines) if lines else "  (no existing tasks)"


async def _get_relevant_existing_tasks(eod_text: str, existing_rows: list[dict], k: int = 12) -> list[dict]:
    """
    Return the top-k existing sheet rows most semantically relevant to this EOD.
    Falls back to returning all rows if embedding is unavailable.
    """
    from app.embeddings import find_top_k

    if not existing_rows:
        return []

    row_names = [r.get("sprint_backlog", "") for r in existing_rows if r.get("sprint_backlog")]
    if not row_names:
        return existing_rows

    try:
        top = await find_top_k(eod_text, row_names, k=k)
        top_names = {name for name, _ in top}
        filtered = [r for r in existing_rows if r.get("sprint_backlog") in top_names]
        return filtered if filtered else existing_rows
    except Exception as e:
        logger.warning("Semantic context filtering failed: %s — using all rows", e)
        return existing_rows


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


async def parse_eod(eod_text: str, context: dict) -> list[dict]:
    """
    AI-first parsing: Gemini → Groq → local regex fallback.

    AI drives both task identification and field values.
    Existing rows are semantically filtered to the most relevant before
    being passed to the prompt, so AI gets focused context not noise.

    Returns a list of task dictionaries.
    """
    from app.local_parser import parse_eod_local

    # Narrow existing rows to the most semantically relevant for this EOD
    existing_rows = context.get("existing_rows", [])
    if existing_rows:
        relevant_rows = await _get_relevant_existing_tasks(eod_text, existing_rows)
        context = {**context, "existing_rows": relevant_rows}
        logger.info(
            "Semantic context: %d/%d existing rows selected for prompt",
            len(relevant_rows), len(existing_rows),
        )

    # Step 1: Gemini
    if settings.GEMINI_API_KEY:
        try:
            tasks = await parse_with_gemini(eod_text, context)
            if tasks:
                logger.info("Gemini parsed %d tasks", len(tasks))
                return tasks
            logger.warning("Gemini returned empty, trying Groq")
        except Exception as e:
            logger.warning("Gemini failed: %s — trying Groq", e)

    # Step 2: Groq
    if settings.GROQ_API_KEY:
        try:
            tasks = await parse_with_groq(eod_text, context)
            if tasks:
                logger.info("Groq parsed %d tasks", len(tasks))
                return tasks
            logger.warning("Groq returned empty")
        except Exception as e:
            logger.warning("Groq failed: %s — falling back to local parser", e)

    # Step 3: Local regex fallback
    logger.info("AI unavailable — falling back to local parser")
    return await parse_eod_local(eod_text, context)
