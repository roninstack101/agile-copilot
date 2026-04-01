"""
Teams message capture — HTML stripping, EOD validation, metadata extraction.
"""

import re
from bs4 import BeautifulSoup


def strip_html(raw: str) -> str:
    """
    Strip HTML tags from a Teams rich-text message and return clean plain text.
    Teams sends messages as HTML (e.g. <p>, <br>, <div>, <ul>/<li>).
    """
    if not raw:
        return ""

    soup = BeautifulSoup(raw, "html.parser")

    # Replace <br> with newlines
    for br in soup.find_all("br"):
        br.replace_with("\n")

    # Handle <li> items: Teams sometimes wraps multi-line content in a single <li>
    # with <br> line breaks inside. In that case, the inner lines already have
    # their own bullet prefixes from the user's original text — don't add another.
    for li in soup.find_all("li"):
        li_text = li.get_text()
        # If the <li> contains multiple lines (from <br>), don't add bullet prefix —
        # the content already has its own structure
        if "\n" in li_text:
            li.insert_before("\n")
        else:
            li.insert_before("\n- ")
        li.unwrap()

    # Replace block-level tags with newlines
    for tag in soup.find_all(["p", "div"]):
        tag.insert_before("\n")
        tag.unwrap()

    text = soup.get_text()

    # Clean non-breaking spaces and normalize whitespace
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def validate_eod(text: str) -> bool:
    """
    Check if the text looks like a valid EOD message.
    Accepts bullet-point lists OR plain multi-line task lists.
    """
    if not text or len(text.strip()) < 10:
        return False

    # Bullet or numbered list
    bullet_pattern = re.compile(r"^\s*[-•*]\s*(.+)", re.MULTILINE)
    numbered_pattern = re.compile(r"^\s*\d+[.)]\s*(.+)", re.MULTILINE)

    bullets = bullet_pattern.findall(text)
    numbered = numbered_pattern.findall(text)

    if len(bullets) + len(numbered) >= 1:
        return True

    # Plain multi-line: at least 2 non-empty content lines (excluding header/greeting)
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    content_lines = [
        l for l in lines
        if not re.match(r"^(mon|tue|wed|thu|fri|sat|sun)", l, re.IGNORECASE)
        and "eod" not in l.lower()
    ]
    return len(content_lines) >= 2


def extract_metadata(payload: dict) -> dict:
    """
    Extract structured metadata from a Teams webhook / Graph API payload.

    Expected payload shape (from Graph API subscription notification):
    {
        "from": {"user": {"displayName": "Aarav Sharma"}},
        "body": {"content": "<p>Wednesday EOD</p><ul><li>task 1</li></ul>"},
        "createdDateTime": "2025-01-15T18:30:00Z"
    }

    Returns:
    {
        "sender": "Aarav Sharma",
        "raw_message": "<p>...</p>",
        "clean_message": "Wednesday EOD\n- task 1",
        "timestamp": "2025-01-15T18:30:00Z"
    }
    """
    # Extract sender name
    sender = "Unknown"
    if "from" in payload:
        from_obj = payload["from"]
        if isinstance(from_obj, dict):
            user = from_obj.get("user", {})
            sender = user.get("displayName", "Unknown") if isinstance(user, dict) else "Unknown"
        elif isinstance(from_obj, str):
            sender = from_obj

    # Legacy flat field (from Power Automate payloads)
    if sender == "Unknown":
        sender = payload.get("sender", "Unknown")

    # Extract message body
    raw_message = ""
    if "body" in payload:
        body = payload["body"]
        if isinstance(body, dict):
            raw_message = body.get("content", "")
        elif isinstance(body, str):
            raw_message = body
    elif "message" in payload:
        raw_message = payload["message"]

    # Extract timestamp
    timestamp = payload.get("createdDateTime", payload.get("timestamp", ""))

    clean_message = strip_html(raw_message)

    return {
        "sender": sender,
        "raw_message": raw_message,
        "clean_message": clean_message,
        "timestamp": timestamp,
    }


def is_eod_message(text: str) -> bool:
    """
    Check if a message is an EOD update.
    Only triggers on the 'eod' keyword (case-insensitive).
    """
    if not text:
        return False
    return "eod" in text.lower()
