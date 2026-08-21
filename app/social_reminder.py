"""
Social-media reminder — builds tomorrow's posting reminder from the shared
content calendar, rendered with a fixed deterministic template so the
message is identical in structure and wording every time it's sent.
"""

import logging

from app.social_excel import get_tomorrow_content

logger = logging.getLogger(__name__)

_FIELD_LABELS = {
    "type": "Type",
    "story": "Story",
    "title": "Title",
    "caption": "Caption",
    "description": "Description",
    "note": "Note",
}


def _render_html(data: dict) -> str:
    """Plain HTML rendering — same structure and wording on every run."""
    lines = [f"<b>📱 Social Media Reminder — {data['weekday']}, {data['date']}</b><br><br>"]
    for brand, info in data["brands"].items():
        header = f"<b>{brand}</b>"
        if info.get("festival"):
            header += f" <i>({info['festival']})</i>"
        items = info["items"]
        if not items:
            lines.append(f"{header} — nothing scheduled.<br>")
            continue
        lines.append(header + "<br>")
        for item in items:
            parts = [item["platform"]]
            for key, label in _FIELD_LABELS.items():
                if item.get(key):
                    parts.append(f"{label}: {item[key]}")
            lines.append("&bull; " + " — ".join(parts) + "<br>")
    return "".join(lines)


async def run_once() -> dict:
    """Build tomorrow's social-media reminder. Does not send it."""
    data = await get_tomorrow_content()
    html = _render_html(data)
    count = sum(len(info["items"]) for info in data["brands"].values())
    return {"date": data["date"], "count": count, "html": html}
