# Agile Copilot — Implementation Guide

## Project overview

Agile Copilot is an automated AI agent that captures daily EOD (End of Day) updates from Microsoft Teams and fills the team's agile Excel sheet on SharePoint/OneDrive — eliminating the manual double-entry where team members post their EOD *and* update the sheet separately.

The pipeline: **MS Teams EOD → Webhook → AI Parser → Validation → Excel Writer → Confirmation**

---

## Problem statement

The current workflow has a redundancy problem. Team members post their EOD in an MS Teams channel (a quick, natural habit), then separately open an Excel sheet and manually fill in the same information across 11 columns. This leads to inconsistent data (sheet says "WIP" but EOD says "submitted for review"), forgotten entries, and wasted time. The agile sheet should be a *byproduct* of the EOD — not a separate task.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  MS Teams Channel                                               │
│  Team member posts EOD message                                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │ Trigger (webhook / Power Automate)
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  Webhook Listener (Cloud Server)                                │
│  Receives: message body, sender name, timestamp                 │
│  Strips HTML, validates format, extracts metadata               │
└──────────────────────┬──────────────────────────────────────────┘
                       │ Raw text + context
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  AI Agent (Free LLM)                                            │
│  Input: EOD text + backlog context + sprint dates + rules       │
│  Output: JSON array of structured task objects                  │
│  Primary: Gemini 1.5 Flash | Fallback: Groq (Llama 3.1 70B)   │
└──────────────────────┬──────────────────────────────────────────┘
                       │ Parsed JSON tasks
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  Validation Layer                                               │
│  Dedup, backlog matching, field defaults, schema enforcement    │
└──────────────────────┬──────────────────────────────────────────┘
                       │ Validated rows
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  Excel Writer (Microsoft Graph API)                             │
│  Appends/updates rows in the agile sheet on SharePoint/OneDrive │
└──────────────────────┬──────────────────────────────────────────┘
                       │ Confirmation
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  Teams Notification                                             │
│  Sends confirmation message back to the member                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Agile sheet columns (target schema)

| # | Column | Type | Auto-filled by AI | Notes |
|---|--------|------|-------------------|-------|
| 1 | Backlog | Text | Yes (if matched) | Original backlog task name if task was pre-assigned |
| 2 | Sprint Backlog | Text | Yes | Clean task name extracted from EOD |
| 3 | Dependency | Text | Yes (if detected) | Normalized: "Team/Person – what's needed" |
| 4 | Deadline | Date | Yes (default) | Defaults to current sprint end date |
| 5 | Priority | Enum | Yes (inferred) | High / Medium / Low — inferred from keywords |
| 6 | WIP | Yes/blank | Yes | Marked "Yes" when stage is WIP (default) |
| 7 | Sent for Approval | Yes/blank | Yes | Marked "Yes" when stage keywords detected |
| 8 | Closed | Yes/blank | Yes | Marked "Yes" when completion keywords detected |
| 9 | Comments | Text | Yes | Context: "From backlog", "Adhoc task", "Quantity: 2", etc. |
| 10 | Expected Story Points | Number | Yes (estimated) | AI estimates 1–13 based on complexity signals |
| 11 | Actual Story Points | Number | No | Always 0 on creation — member fills manually |

---

## Component details

### 1. MS Teams message capture

There are four ways to capture EOD messages from Teams. Here they are ranked by fit for this use case:

**Option A: Power Automate (recommended)**

Best for teams with M365 licenses. Zero code on the Microsoft side.

Setup:
1. Open Power Automate → Create → Automated cloud flow
2. Trigger: "When a new message is posted in a channel"
3. Select your team and the specific EOD channel
4. Add a Condition action: check if message body contains "EOD" (case-insensitive)
5. If yes → HTTP POST action to `https://your-server.com/api/eod-webhook`
6. Payload: `{ "sender": @{triggerBody()?['from']?['user']?['displayName']}, "message": @{triggerBody()?['body']?['content']}, "timestamp": @{triggerBody()?['createdDateTime']} }`

Pros: no code, built-in retry, handles auth automatically.
Cons: free tier capped at ~600 runs/month, message arrives as HTML (need to strip tags server-side).

**Option B: Outgoing webhook**

Simplest setup. Team members @mention a bot name to trigger it.

Setup:
1. Go to Teams → Manage team → Apps → Create outgoing webhook
2. Set name (e.g., "AgileCopilot") and callback URL
3. Team members write: `@AgileCopilot Wednesday EOD - task 1 - task 2`

Pros: 2-minute setup, no Azure AD needed, instant delivery.
Cons: requires @mention (slight behavior change), only triggers on mentions.

**Option C: Microsoft Graph API subscriptions**

Developer-first approach. Full control, passive listening.

Setup:
1. Register an Azure AD app with `ChannelMessage.Read.All` permission
2. Get admin consent for the application permission
3. POST to `/subscriptions` with resource: `/teams/{team-id}/channels/{channel-id}/messages`
4. Handle validation handshake (echo back `validationToken`)
5. Renew subscription every 60 minutes (max lifetime for channel messages)

Pros: full control, access to reactions/replies/attachments, no Power Automate dependency.
Cons: complex setup, subscription renewal loop needed, admin consent required.

**Option D: Bot Framework (Azure Bot Service)**

Full bot that lives in the channel. Richest interaction model.

Pros: can reply with adaptive cards for task confirmation before writing, professional-grade.
Cons: most complex to build, overkill for passive capture.

**Recommendation**: Start with Power Automate. Move to Graph API subscriptions if you need more control later.

---

### 2. Webhook listener (cloud server)

A lightweight HTTP server that receives the Teams payload, cleans it, and kicks off the pipeline.

**What it does:**
- Receives POST request with sender, message HTML, timestamp
- Strips HTML tags from message body (Teams sends rich text)
- Validates the message looks like an EOD (contains bullet points / task lines)
- Extracts the member name and maps it to their sheet tab/row range
- Fetches current backlog for that member from the Excel sheet (via Graph API)
- Sends the cleaned text + context to the AI agent
- Receives parsed tasks, runs validation, writes to sheet, sends confirmation

**Tech choices:**
- Python (FastAPI or Flask) — simplest for this use case
- Node.js (Express) — if team prefers JS
- Deployed on: Railway (free tier), Render (free tier), Azure Functions (consumption plan), or Cloudflare Workers

**Server endpoint structure:**

```
POST /api/eod-webhook
  → receive payload
  → clean HTML
  → validate EOD format
  → fetch backlog context from sheet
  → call AI agent
  → validate parsed tasks
  → write to Excel via Graph API
  → send Teams confirmation
  → return 200 OK
```

---

### 3. AI agent (free LLM)

The core parser that converts natural-language EOD into structured task data.

**Primary: Google Gemini 1.5 Flash**

Why: free tier gives 15 req/min and 1M tokens/day (your usage will be <1% of this). Supports JSON mode — the model is constrained to return valid JSON matching your schema, eliminating the biggest failure mode.

API call structure:
```
POST https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent
Headers: Content-Type: application/json
Params: key=YOUR_API_KEY
Body: {
  "contents": [{ "parts": [{ "text": "<system prompt + EOD text + context>" }] }],
  "generationConfig": {
    "responseMimeType": "application/json",
    "responseSchema": { <your task schema> }
  }
}
```

**Fallback: Groq (Llama 3.1 70B)**

Why: near-instant inference (<1s), free tier with 30 req/min. Llama 3.1 70B follows structured output instructions well.

API call structure:
```
POST https://api.groq.com/openai/v1/chat/completions
Headers: Authorization: Bearer YOUR_API_KEY, Content-Type: application/json
Body: {
  "model": "llama-3.1-70b-versatile",
  "messages": [
    { "role": "system", "content": "<system prompt>" },
    { "role": "user", "content": "<EOD text + context>" }
  ],
  "response_format": { "type": "json_object" }
}
```

**Last-resort fallback: local regex parser**

If both LLMs fail, a simple rule-based parser handles the 80% case: split by bullet points, apply keyword matching for priority/stage, use defaults for everything else. No AI needed — just string manipulation.

---

### 4. System prompt

The system prompt is the backbone of the parsing accuracy. It must cover:

**Role definition:**
```
You are an agile task parser. You receive raw EOD text from a team member.
You return ONLY a valid JSON array. No explanations, no markdown, no preamble.
If you cannot parse a line, skip it. Never invent tasks not in the EOD.
```

**Column mapping rules:**

- Backlog: match against provided backlog list (fuzzy). If match found, use exact backlog name. If no match, leave empty.
- Sprint Backlog: always populate. Clean task name — remove "Adhoc:" prefix, "x2" suffix, dependency parentheticals.
- Dependency: extract from parentheses mentioning "waiting", "blocked", "needs", "dependent on". Normalize to "Team/Person – description". Empty if none.
- Deadline: use the sprint end date provided in context. Don't infer from EOD text.
- Priority: "urgent/critical/ASAP/blocker/P0" → High. "minor/low/nice to have" → Low. Default → Medium.
- Stage (WIP/Sent for Approval/Closed): "submitted/sent for review/shared with manager" → Sent for Approval. "done/completed/finished/delivered" → Closed. Default → WIP.
- Comments: "From backlog" if matched, "Adhoc task" if adhoc, "Quantity: N" if multiplier present.
- Expected Story Points: simple fix/change → 1, standard task → 2, setup/testing/multi-part → 3, large feature → 5. Multiply by quantity. Clamp 1–13.
- Actual Story Points: always 0.

**Parsing rules:**

- Each line starting with -, •, or * is one task
- First line (day/date header like "Wednesday EOD") — skip
- "Adhoc:" or "Ad-hoc:" prefix (case insensitive) → flag as adhoc, strip prefix
- "x2", "x3" at end of line → quantity marker, include in comments
- Text in parentheses with dependency keywords → extract as dependency
- Lines without bullet prefix after header → continuation of previous task, not new task

**Edge case rules:**

- Empty or greeting-only EOD → return empty array `[]`
- Ambiguous lines → include as task with comment "Ambiguous — verify"
- Never hallucinate tasks not in the input
- Never merge two bullet points into one task

**Context injected per request (dynamic):**

- Current backlog list for the member
- Current sprint end date (mid-month or end-of-month)
- Member name
- Today's date

---

### 5. Validation layer

Runs after AI parsing, before writing to the sheet. Pure logic, no AI.

**Rule 1: Deduplication**
Compare each parsed task against existing sheet rows for the same member using fuzzy string matching (e.g., Levenshtein ratio ≥ 0.85). If a match is found, update the existing row's stage/comments instead of creating a duplicate.

**Rule 2: Backlog matching**
If a parsed task fuzzy-matches an item in the Backlog column, copy the exact backlog name (for consistency), set the Backlog field, and add "From backlog" to Comments.

**Rule 3: Field defaults**
- Stage: must be exactly one of WIP / Sent for Approval / Closed. Default WIP.
- Priority: must be High / Medium / Low. Default Medium.
- Deadline: must be a valid date. Default to sprint end date.
- Expected SP: must be integer 1–13. Clamp if out of range. Default 2.
- Actual SP: force to 0 on creation.

**Rule 4: Adhoc verification**
If AI flagged a task as adhoc but it matches a backlog item, remove the adhoc flag — it was pre-assigned work, not unplanned.

**Rule 5: Dependency normalization**
Standardize dependency text to "Team/Person – description" format. Strip leading/trailing whitespace, capitalize team names.

**Rule 6: Same-day conflict detection**
If the same member has already submitted an EOD today, merge new tasks with existing ones. Don't duplicate the entire set.

**Rule 7: Schema enforcement**
Every row must have exactly 11 fields in the correct column order. No field exceeds 500 characters. All fields are strings or numbers as expected.

---

### 6. Excel writer (Microsoft Graph API)

Writes validated task rows to the agile sheet on SharePoint or OneDrive.

**Setup:**
1. Register an Azure AD app (or reuse the one from Graph subscriptions)
2. Grant permissions: `Files.ReadWrite.All` (application) or `Files.ReadWrite` (delegated)
3. Get the workbook's drive item ID from SharePoint/OneDrive

**Key Graph API endpoints:**

Read existing sheet data (for dedup + backlog context):
```
GET /drives/{drive-id}/items/{item-id}/workbook/worksheets/{sheet-name}/usedRange
Authorization: Bearer {token}
```

Append new rows:
```
POST /drives/{drive-id}/items/{item-id}/workbook/worksheets/{sheet-name}/tables/{table-name}/rows/add
Authorization: Bearer {token}
Body: {
  "values": [
    ["backlog_val", "sprint_backlog_val", "dependency_val", "deadline_val", "priority_val", "Yes", "", "", "comment_val", 2, 0]
  ]
}
```

Update existing row (for dedup merge):
```
PATCH /drives/{drive-id}/items/{item-id}/workbook/worksheets/{sheet-name}/range(address='A{row}:K{row}')
Authorization: Bearer {token}
Body: {
  "values": [["updated_backlog", "updated_sprint_backlog", ...]]
}
```

**Auth flow:**
Use client credentials flow (app-only, no user login needed):
```
POST https://login.microsoftonline.com/{tenant-id}/oauth2/v2.0/token
Body: client_id, client_secret, scope=https://graph.microsoft.com/.default, grant_type=client_credentials
```

Cache the token (valid for ~1 hour), refresh before expiry.

---

### 7. Teams notification

After the sheet is updated, send a confirmation back to the member in the Teams channel.

**Using Power Automate:** Add a "Post a message" action at the end of your flow.

**Using Graph API (if using Option C for capture):**
```
POST /teams/{team-id}/channels/{channel-id}/messages
Authorization: Bearer {token}
Body: {
  "body": {
    "contentType": "html",
    "content": "✓ <b>4 tasks</b> added to your sprint backlog, Aarav.<br>• Schneider Catalogue Content changes (WIP, Medium)<br>• Banner reference docs x2 (WIP, Medium)<br>• Brand Identity Setup Testing (WIP, Medium)<br>• Quick fix on homepage slider (Adhoc, WIP, Medium)"
  }
}
```

**Using incoming webhook (simplest for notifications only):**
1. In Teams, go to channel → Connectors → Incoming Webhook → Configure
2. Copy the webhook URL
3. POST a JSON payload to that URL from your server:
```
POST {webhook-url}
Body: {
  "@type": "MessageCard",
  "summary": "Agile sheet updated",
  "sections": [{
    "activityTitle": "✓ EOD processed for Aarav Sharma",
    "facts": [
      { "name": "Tasks added", "value": "4" },
      { "name": "Adhoc tasks", "value": "1" },
      { "name": "Total story points", "value": "8" }
    ]
  }]
}
```

---

## Deployment options

### Option 1: Railway / Render (recommended for getting started)

Deploy a Python FastAPI app. Both have free tiers.

Stack: Python 3.11 + FastAPI + httpx (for API calls) + fuzzywuzzy (for string matching)

Deployment:
1. Push code to GitHub
2. Connect repo to Railway or Render
3. Set environment variables: `GEMINI_API_KEY`, `GROQ_API_KEY`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `TEAMS_WEBHOOK_URL`, `DRIVE_ITEM_ID`, `SHEET_NAME`
4. Deploy — the service gets a public URL for your webhook endpoint

Pros: simple, free, auto-deploys on git push.
Cons: free tier may sleep after inactivity (cold starts add 5–10s latency).

### Option 2: Azure Functions (serverless)

Best if your org is already on Azure.

Stack: Python Azure Function with HTTP trigger

Pros: pay-per-execution (essentially free at this volume), no server to manage, native Azure AD integration.
Cons: Azure setup has more moving parts, cold starts on consumption plan.

### Option 3: Cloudflare Workers

Best for minimal infrastructure.

Stack: JavaScript Worker + Cloudflare KV (for caching tokens/backlog)

Pros: runs at the edge (fast globally), generous free tier (100K requests/day), can combine listener + parser + writer in one Worker.
Cons: limited to JavaScript/WASM, 10ms CPU time limit on free tier (tight for complex parsing), no native Python.

### Option 4: Self-hosted (VM / VPS)

Deploy on a small VM (e.g., Oracle Cloud free tier, AWS t2.micro free tier).

Pros: full control, no cold starts, can run cron jobs for backlog sync.
Cons: you manage the server, updates, uptime.

**Recommendation**: Start with Railway or Render. Migrate to Azure Functions if your org requires it.

---

## Project file structure

```
agile-copilot/
├── app/
│   ├── main.py                 # FastAPI app, webhook endpoint
│   ├── config.py               # Environment variables, constants
│   ├── teams_capture.py        # HTML stripping, message validation
│   ├── ai_parser.py            # LLM calls (Gemini primary, Groq fallback)
│   ├── local_parser.py         # Regex fallback parser (no AI)
│   ├── validator.py            # Dedup, defaults, schema enforcement
│   ├── excel_writer.py         # Graph API read/write operations
│   ├── notifier.py             # Teams confirmation messages
│   ├── graph_auth.py           # Azure AD token management
│   └── prompts/
│       └── system_prompt.txt   # The full system prompt for the AI agent
├── tests/
│   ├── test_parser.py          # Test EOD parsing with sample inputs
│   ├── test_validator.py       # Test validation rules
│   └── sample_eods.json        # Sample EOD messages for testing
├── requirements.txt
├── Dockerfile
├── railway.toml / render.yaml
└── README.md
```

---

## Sprint-wise implementation plan

### Sprint 1 (Week 1–2): Foundation

- Set up the FastAPI server with the webhook endpoint
- Implement HTML stripping and EOD format validation
- Write the system prompt and test it against 10–15 sample EODs manually
- Implement the Gemini API integration with JSON mode
- Implement the Groq fallback
- Implement the local regex fallback parser
- Deploy to Railway/Render with a test endpoint

### Sprint 2 (Week 3–4): Excel integration

- Register Azure AD app and configure Graph API permissions
- Implement token management (client credentials flow, caching, refresh)
- Implement reading the current sheet state (backlog, existing rows)
- Implement appending new rows via Graph API
- Implement updating existing rows (for dedup merge)
- Test end-to-end: manual POST to webhook → sheet updated

### Sprint 3 (Week 5–6): Teams integration

- Set up Power Automate flow (or outgoing webhook) to capture EODs
- Implement the Teams notification (confirmation message back to member)
- Connect the full pipeline: Teams → webhook → AI → validation → sheet → confirmation
- Test with real EOD messages from 1–2 team members

### Sprint 4 (Week 7–8): Hardening

- Add logging and error alerting (failed parses, API errors)
- Add same-day conflict detection and merge logic
- Add backlog sync (periodically read the full backlog to keep context fresh)
- Edge case testing: empty EODs, malformed messages, duplicate submissions
- Monitor and tune: check parsed output quality, adjust system prompt if needed
- Roll out to the full team

---

## EOD format guide for the team

Share this with team members so they know what the bot understands:

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

The bot understands:
- Standard bullet points (-, •, *)
- "Adhoc:" prefix for unplanned tasks
- "x2", "x3" for repeated/multi-quantity work
- "(dependency: ...)" for blocked items
- Keywords like "submitted for review" → Sent for Approval
- Keywords like "done", "completed" → Closed
- Everything else defaults to WIP, Medium priority

---

## Monitoring and maintenance

**What to monitor:**
- Parse success rate: what % of EODs produce valid JSON on the first LLM attempt
- Fallback rate: how often Groq is used vs Gemini, how often local parser kicks in
- Sheet write errors: Graph API failures (token expiry, permission issues, rate limits)
- Task quality: periodic manual review — are parsed tasks accurate, are priorities reasonable

**Maintenance tasks:**
- Refresh the backlog context before each sprint (or auto-sync daily)
- Update the system prompt if the team's EOD format evolves
- Renew Azure AD client secret before expiry (default 2 years)
- Monitor LLM provider free tier changes (Gemini/Groq may adjust limits)

---

## Cost estimate

| Component | Cost |
|-----------|------|
| Gemini 1.5 Flash API | Free (15 req/min, 1M tokens/day) |
| Groq API (fallback) | Free (30 req/min, daily token limit) |
| Railway / Render hosting | Free tier (with cold start trade-off) |
| Microsoft Graph API | Free (included with M365 license) |
| Power Automate | Free tier (~600 runs/month) |
| Azure AD app registration | Free |
| **Total** | **$0/month** |

If the team grows beyond free tier limits, the first upgrade is hosting (Railway Pro at ~$5/month) and potentially Gemini's pay-as-you-go tier (negligible cost at this volume).

---

## Ways to extend this later

1. **Manager dashboard**: a weekly summary bot that posts sprint progress in a manager channel — total tasks, completion rate, adhoc ratio, story point velocity
2. **Smart priority escalation**: if a task stays in WIP past 70% of the sprint, auto-bump priority to High and notify the member
3. **Standup summary**: before each scrum meeting, the bot generates a per-member summary from the sheet — what's done, what's in progress, what's blocked
4. **Retrospective data**: at sprint end, auto-generate a retro report — planned vs actual story points, adhoc task ratio, dependency bottlenecks
5. **Voice EOD**: integrate with Teams meeting transcripts so members can give verbal EODs during standups and the bot parses the transcript
6. **Multi-sheet support**: if different teams use different sheet formats, make the column mapping configurable per team
