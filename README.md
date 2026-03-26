# Agile Copilot

Automated AI agent that captures MS Teams EOD (End-of-Day) updates and fills your team's agile Excel sheet on SharePoint/OneDrive — no more manual double-entry.

**Pipeline:** MS Teams EOD → Graph API Subscription → AI Parser → Validation → Excel Writer

## Quick Start

### 1. Prerequisites

- Python 3.11+
- An Azure AD app registration with these permissions:
  - `Chat.Read.All` (application) — to read Teams group chat messages
  - `Files.ReadWrite.All` (application) — to read/write Excel on SharePoint/OneDrive
- An Excel workbook on SharePoint/OneDrive with a structured table matching the [agile sheet schema](#agile-sheet-columns)
- API keys for [Google Gemini](https://ai.google.dev/) and/or [Groq](https://console.groq.com/)

### 2. Install

```bash
git clone <your-repo-url>
cd agile-copilot
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Fill in your API keys, Azure AD credentials, and Excel file details
```

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google Gemini API key |
| `GROQ_API_KEY` | Groq API key (fallback) |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_CLIENT_ID` | Azure AD app client ID |
| `AZURE_CLIENT_SECRET` | Azure AD app client secret |
| `DRIVE_ID` | OneDrive/SharePoint drive ID |
| `DRIVE_ITEM_ID` | Excel workbook drive item ID |
| `SHEET_NAME` | Worksheet name (default: `Sheet1`) |
| `TABLE_NAME` | Table name in the sheet (default: `Table1`) |
| `CHAT_ID` | MS Teams group chat ID for EOD messages |
| `WEBHOOK_NOTIFICATION_URL` | Your server's public URL for Graph API notifications |

### 4. Run

```bash
# Development
uvicorn app.main:app --reload

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 5. Test

```bash
python -m pytest tests/ -v
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/eod-webhook` | Receive a direct EOD payload |
| `GET` | `/api/graph-webhook` | Graph API subscription validation handshake |
| `POST` | `/api/graph-webhook` | Receive Graph API notifications for new messages |
| `POST` | `/api/subscribe` | Create/renew Graph API subscription |
| `GET` | `/health` | Health check |

### Send a test EOD

```bash
curl -X POST http://localhost:8000/api/eod-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "sender": "Aarav Sharma",
    "message": "Wednesday EOD\n- Schneider Catalogue Content changes\n- Banner reference docs x2\n- Adhoc: Homepage slider fix\n- Email template — done",
    "timestamp": "2025-01-15T18:30:00Z"
  }'
```

## Agile Sheet Columns

| # | Column | Type | Auto-filled |
|---|--------|------|-------------|
| 1 | Backlog | Text | Yes (if matched) |
| 2 | Sprint Backlog | Text | Yes |
| 3 | Dependency | Text | Yes (if detected) |
| 4 | Deadline | Date | Yes (sprint end) |
| 5 | Priority | High/Medium/Low | Yes |
| 6 | WIP | Yes/blank | Yes |
| 7 | Sent for Approval | Yes/blank | Yes |
| 8 | Closed | Yes/blank | Yes |
| 9 | Comments | Text | Yes |
| 10 | Expected Story Points | 1–13 | Yes |
| 11 | Actual Story Points | Number | No (manual) |

## Architecture

```
Group Chat Message → Graph API Subscription → Webhook Listener
    → HTML Strip → EOD Validation
    → AI Parser (Gemini → Groq → Local Regex)
    → Validation (Dedup, Backlog Match, Defaults, Schema)
    → Excel Writer (Graph API)
```

## Deployment

### Railway

```bash
# Push to GitHub, connect repo in Railway
# Set env vars in Railway dashboard
# Auto-deploys on push
```

### Docker

```bash
docker build -t agile-copilot .
docker run -p 8000:8000 --env-file .env agile-copilot
```

## EOD Format Guide

Share with your team — the bot understands:

```
Wednesday EOD
- Schneider Catalogue Content changes
- Banner reference docs x2
- Brand Identity Setup Testing
- Adhoc: Quick fix on homepage slider
- Product page layout revision (dependency: waiting for assets from Design team)
- Email template — submitted for review
- Social media creatives — done
```

- `- `, `• `, `* ` — bullet points (each = one task)
- `Adhoc:` prefix — marks unplanned work
- `x2`, `x3` — quantity markers
- `(dependency: ...)` — blocked items
- `submitted for review` → Sent for Approval
- `done` / `completed` → Closed
- Everything else → WIP, Medium priority
