# Agile Copilot — Project Documentation

## Overview

Agile Copilot is an automated agile assistant for the World Goods Market team. It listens to a Microsoft Teams group chat, detects EOD (End of Day) updates sent by team members, parses them into structured tasks, and writes them directly into each member's worksheet in a shared SharePoint Excel workbook — without anyone needing to manually fill the agile sheet.

---

## Architecture

```
Teams Group Chat
       │
       │  (Graph API Subscription)
       ▼
  AWS EC2 Server  ◄──── nginx (HTTPS via Let's Encrypt + DuckDNS)
       │
       ▼
  FastAPI App (uvicorn, port 8080)
       │
       ├── Teams message received
       ├── EOD detection
       ├── Parsing (Gemini AI → Groq fallback → Local regex)
       ├── Validation & Backlog Promotion
       └── Write to SharePoint Excel (Microsoft Graph API)
```

---

## Infrastructure

| Component | Detail |
|-----------|--------|
| Server | AWS EC2 t2.micro (free tier) |
| OS | Ubuntu 22.04 |
| Domain | agile-copilot.duckdns.org (DuckDNS free domain) |
| SSL | Let's Encrypt via certbot |
| Reverse proxy | nginx (ports 80/443 → 8080) |
| App server | uvicorn (FastAPI) |
| Process manager | systemd (`agile-copilot.service`) |
| Code repository | GitHub (roninstack101/agile-copilot) |
| Deployment | `git pull` + `systemctl restart agile-copilot` |

---

## Environment Variables (`.env`)

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google Gemini 2.5 Flash API key |
| `GROQ_API_KEY` | Groq API key (fallback AI) |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_CLIENT_ID` | Azure app registration client ID |
| `AZURE_CLIENT_SECRET` | Azure app registration client secret |
| `DRIVE_ID` | OneDrive/SharePoint drive ID (the Excel file's drive) |
| `DRIVE_ITEM_ID` | Excel workbook item ID |
| `SHEET_NAME` | Default worksheet name |
| `CHAT_ID` | Teams group chat ID to monitor |
| `WEBHOOK_NOTIFICATION_URL` | Public URL for Graph API to POST notifications to |
| `REDIRECT_URI` | OAuth callback URL for delegated auth login |

---

## Full Pipeline Flow

### 1. Teams Message Reception

Graph API sends a POST notification to `/api/graph-webhook` whenever a new message is posted in the subscribed Teams chat.

- Self-loop prevention: skips messages sent by the bot itself (matched by `AZURE_CLIENT_ID`) or bot-generated messages (matched by signature keywords like "Good Morning! Daily Focus")
- Dedup cache: `_processed_messages` set prevents processing the same notification twice

### 2. Command Detection

Before EOD processing, the message is checked for special commands:

- **`/backlog`** → adds items to the sender's Backlog column (see Commands section)
- Not an EOD → skipped

### 3. EOD Detection

`is_eod_message()` checks if the message contains the keyword **"eod"** (case-insensitive). Any message without "eod" is ignored.

`validate_eod()` checks the message has actual task content (bullet points, numbered list, or 2+ content lines).

### 4. Parsing (3-tier fallback)

**Tier 1 — Gemini 2.5 Flash (AI)**
Sends the EOD text to Google Gemini with a structured system prompt. Returns a JSON list of tasks with brand, activity_type, stage, dependency fields.

**Tier 2 — Groq (AI fallback)**
If Gemini fails, retries with Groq's LLaMA model using the same prompt.

**Tier 3 — Local regex parser**
If both AIs fail, the local parser handles parsing using:
- Bullet/numbered list extraction
- Brand detection (keyword matching against `KNOWN_BRANDS`)
- Activity type detection (keyword sets)
- Stage detection: "done/completed/finished" → Closed, "review pending/sent for approval" → Sent for Approval, else WIP
- Progress prefix stripping: "Worked on / Working on / Started working on" removed
- Completion suffix stripping: "done / completed / finished" removed from end
- Parenthetical notes → moved to Comments column
- Quantity extraction: "x2", "x3" → `Quantity: N` in comments

**AI enrichment (hybrid mode)**
After local parsing extracts structure, AI enriches each task with:
- Brand (if not detected locally)
- Activity type (if not detected locally)
- Stage
- Dependency

AI does NOT set: priority, story points, comments.

### 5. Validation

`validate_all()` runs on all parsed tasks:

1. **Backlog matching** — fuzzy-matches task name against backlog items (backlog list from sheet). If matched, adds "From backlog" to comments.
2. **Adhoc verification** — tags tasks with "Adhoc task" in comments if no backlog match and no brand.
3. **Dependency normalization** — cleans up dependency text.
4. **Defaults** — fills missing fields (deadline = sprint end date).
5. **Schema enforcement** — clamps field lengths, validates enums.

### 6. Backlog Promotion (Task Router)

`route_tasks()` fuzzy-matches each task against the sheet's **Backlog column** items (threshold: 65%).

- **Match found** → task is written in-place on the existing backlog row (updates that row with Sprint Backlog, stage, brand, etc.)
- **No match** → task is queued for append as a new row

### 7. Write to Excel

**In-place backlog updates** (`update_backlog_row`):
Writes the task data directly onto the row where the backlog item exists. Clears the backlog cell, fills Sprint Backlog and all other columns.

**New row inserts** (`write_tasks`):
- Reads the sheet to find the last non-empty row (ignores trailing blank/formatted rows)
- **Brand grouping**: tasks with a known brand are inserted after the last existing row of that same brand — keeping brand sections together
- After each insert, all brand positions are updated to account for row shifts
- Tasks without a brand go to end of sheet
- Formatting applied: fill color (green=Closed, red=Adhoc, grey=WIP), font, alignment

**Story points are never written** — team fills manually.

---

## Sheet Structure

Each team member has their own worksheet. The column layout is auto-detected per sheet:

| Column | Description |
|--------|-------------|
| Brand | Brand/client name (dropdown) |
| Activity Type | Type of work (dropdown) |
| Backlog | Pending items (promoted to sprint) |
| Sprint Backlog | Active sprint task name |
| Dependency | Blocked by / waiting on |
| Deadline | Sprint end date |
| Priority | Left blank (team fills) |
| WIP | Checkbox — task in progress |
| Sent for Approval | Checkbox — awaiting review |
| Closed | Checkbox — completed |
| Comments / Outcome | Notes, parenthetical remarks, "Adhoc task", "From backlog" |
| Expected Story Points | Team fills manually |
| Actual Story Points | Team fills manually |

### Sprint Dates

Sprint end is auto-calculated:
- 1st–15th of month → sprint ends on 15th
- 16th–end of month → sprint ends on last day of month

---

## Scheduled Notifications

All notifications are sent to the Teams group chat. Off-days (Sundays, 1st Saturday, 3rd Saturday) are skipped.

| Time (IST) | Notification |
|------------|-------------|
| 9:30 AM | **Todo Summary** — AI-prioritized top 5 WIP tasks per member |
| 10:15 AM | **Agile Update Reminder** — nudge to update the agile sheet |
| 11:30 AM | **Sprint Progress Report** — actual/expected SP per member |
| 6:00 PM | **EOD Reminder** — asks team to submit end-of-day update |

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/api/eod-webhook` | Manual EOD submission (for testing) |
| GET | `/api/graph-webhook` | Graph API validation handshake |
| POST | `/api/graph-webhook` | Graph API notification receiver |
| POST | `/api/subscribe` | Create Teams chat subscription |
| GET | `/api/login` | Start delegated OAuth login |
| GET | `/api/auth-callback` | OAuth callback (saves token) |
| POST | `/api/morning-summary` | Trigger 9:30 AM todo summary manually |
| POST | `/api/agile-reminder` | Trigger 10:15 AM agile reminder manually |
| POST | `/api/progress-report` | Trigger progress report manually (`?send=false` to preview) |
| POST | `/api/eod-reminder` | Trigger 6 PM EOD reminder manually |
| POST | `/api/test-message` | Send a test message to Teams |
| POST | `/api/notify-wip` | Send WIP task summary |

---

## Teams Commands

### `/backlog`

Team members can add items to their Backlog column directly from Teams chat.

**Single item:**
```
/backlog Brand Film script
```

**Multiple items:**
```
/backlog
- Brand Film script
- Website redesign
- Social media calendar
```

The bot replies with a confirmation. Items are appended to the Backlog column of the sender's sheet and will be automatically promoted to Sprint Backlog when mentioned in a future EOD.

---

## Authentication

### App-only (Client Credentials)
Used for: reading/writing Excel, listing sheets, fetching Teams messages.
- Token fetched via `AZURE_CLIENT_ID` + `AZURE_CLIENT_SECRET`
- Auto-refreshed when expired

### Delegated (OAuth2)
Used for: sending messages to Teams chat (app-only tokens get 403 for chat messages unless the app is installed as a bot in the chat).
- Login once via `https://agile-copilot.duckdns.org/api/login`
- Token saved to `delegated_token.json`, refreshed automatically
- Falls back to app-only if delegated token unavailable

---

## Graph API Subscription

The app subscribes to the Teams group chat to receive notifications on new messages.

- Subscription expires every ~60 minutes (Graph API limit)
- Auto-renewal runs every 55 minutes in the background
- Manual: `POST /api/subscribe`
- Subscription validates the webhook via a GET handshake before activating

---

## Known Brands & Activity Types

**Brands:** Wogom, Wofi, Brandverse, WDV, Mediaverse, WEMS, Schneider, Abaj, Nar Narayan, Internal

**Sub-brands:** Wobble → Wogom, Aiwa → Abaj

**Activity Types:** Social Media, Collateral, Website, Branding, Ops, Content, Digital Marketing

---

## Member Sheets

Active sheets tracked by the system:

| Member | Notes |
|--------|-------|
| Dhwani | Active |
| Shaily | Active |
| Shriya | Active |
| Ravi | Active |
| Prince | Tasks in Backlog column (not Sprint Backlog) |
| Yogini | Active |
| Yash | Active |
| Kriishna | Note: sheet name has double 'i' (Kriishna) |
| Harshil | Included |
| Rinal | Included |

Excluded from all processing: `Sheet1`, `Initiatives`, `Template`

---

## Deployment Guide

### SSH into server
```bash
ssh -i "path/to/agile_copilot_key.pem" ubuntu@agile-copilot.duckdns.org
```

### Deploy latest code
```bash
cd /home/ubuntu/agile-copilot
git pull
sudo systemctl restart agile-copilot
sudo systemctl status agile-copilot
```

### View logs
```bash
sudo journalctl -u agile-copilot -f
sudo journalctl -u agile-copilot -n 100 --no-pager
```

### Edit environment variables
```bash
nano /home/ubuntu/agile-copilot/.env
sudo systemctl restart agile-copilot
```

### Subscribe to Teams chat (after server restart)
```bash
# From your machine:
curl -X POST https://agile-copilot.duckdns.org/api/subscribe
# Or via PowerShell:
Invoke-WebRequest -Method POST -Uri https://agile-copilot.duckdns.org/api/subscribe
```

### Login (delegated auth — needed for sending messages)
Open in browser:
```
https://agile-copilot.duckdns.org/api/login
```

---

## File Structure

```
agile-copilot/
├── app/
│   ├── main.py              # FastAPI app, webhook handler, all endpoints, scheduler wiring
│   ├── config.py            # Settings, constants, brand/activity lists, sprint dates
│   ├── graph_auth.py        # Microsoft Graph auth (app-only + delegated OAuth)
│   ├── excel_writer.py      # All Excel read/write operations via Graph API
│   ├── teams_capture.py     # HTML stripping, EOD detection, metadata extraction
│   ├── ai_parser.py         # Gemini + Groq AI parsing with hybrid local/AI enrichment
│   ├── local_parser.py      # Regex-based fallback parser
│   ├── validator.py         # Task validation, dedup, defaults, schema enforcement
│   ├── task_router.py       # Backlog promotion (fuzzy match tasks to backlog items)
│   ├── scheduler.py         # Daily notification scheduler (IST timezone, off-day aware)
│   └── subscription_manager.py  # Graph API subscription lifecycle management
├── .env                     # Environment variables (not in git)
├── requirements.txt         # Python dependencies
└── DOCUMENTATION.md         # This file
```

---

## Common Issues & Fixes

| Issue | Cause | Fix |
|-------|-------|-----|
| 504 Gateway Timeout | Pipeline takes >60s for large EODs | nginx `proxy_read_timeout 120s` |
| FALSE/FALSE/TRUE in cells | Writing Python `False` to checkbox columns | Write `""` for inactive, `True` for active |
| Blank rows before new entry | `usedRange` includes formatted-but-empty rows | `_last_data_row()` scans backwards |
| Brand grouping broken | Row insert shifts all rows but only current brand position updated | Update all brand positions after each insert |
| 403 on Teams message send | App-only token can't send to chat without bot install | Delegated OAuth flow (send as user) |
| "reached limit of 1 subscription" | Old subscription still active from previous deploy | Delete old subscription via Graph API |
| Backlog promotion always 0 matches | Threshold too strict (was 0.80) | Lowered to 0.65 |
| AI corrupting comments | AI was overwriting local parser's parenthetical notes | Removed `comments` from AI enrichment fields |
