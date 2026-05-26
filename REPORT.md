# Agile Copilot — Project Report

## Executive Summary

The Agile Copilot was built to eliminate the manual overhead of maintaining an agile tracking sheet for a 10-person creative team. Team members were expected to update a shared Excel sheet daily with their tasks, progress, and story points — but this was inconsistently done, leading to outdated sprint boards and poor visibility for project leads.

The solution: an AI-powered bot that listens to the team's existing Microsoft Teams chat, automatically reads EOD (End of Day) updates, extracts structured task data, and writes it directly into each member's worksheet — without changing anyone's existing workflow.

---

## Problem Statement

### 1. Manual Sheet Updates Were Unreliable
Team members were responsible for updating their own worksheet in a shared SharePoint Excel file every day. In practice:
- Updates were often skipped or delayed
- Tasks were written inconsistently (different formats, missing fields)
- Brands, activity types, and story points were often left blank
- Closed tasks were not being marked as closed

### 2. No Visibility Into Sprint Progress
Without consistent updates, the project lead had no real-time view of:
- Which tasks were in progress vs. blocked vs. completed
- Whether the team was on track for the sprint deadline
- How story points were tracking against expectations

### 3. Duplicate Effort
Team members were already sending EOD messages in Teams every day. Having to also update the Excel sheet was a second, redundant step that added friction with no additional value to the sender.

### 4. Backlog Was Disconnected From Daily Work
The Backlog column existed in the sheet but was never connected to sprint planning. Items sat in backlog indefinitely with no automatic promotion when a team member started working on them.

---

## Solution

### Core Concept
Intercept the EOD messages already being sent in Teams, parse them using AI, and write structured data into the Excel sheet automatically. The team keeps doing exactly what they were doing — the bot handles the rest.

### How It Works

1. **Teams Listener** — Microsoft Graph API subscription monitors the agile group chat in real time. Every new message triggers a webhook notification to the server.

2. **EOD Detection** — Messages are checked for the "eod" keyword. Non-EOD messages (announcements, replies, chatter) are ignored.

3. **AI Parsing** — The EOD text is sent to Gemini 2.5 Flash, which extracts structured tasks: name, brand, activity type, stage, dependency. If AI fails, a local regex parser handles it as fallback.

4. **Backlog Promotion** — Parsed tasks are fuzzy-matched against each member's backlog items. If a match is found (≥65% similarity), the task is written onto the existing backlog row rather than creating a new entry.

5. **Brand Grouping** — New tasks are inserted next to other rows of the same brand, keeping the sheet organized by brand section automatically.

6. **Scheduled Notifications** — The bot sends 4 automated messages daily: a todo summary (9:30 AM), an agile update reminder (10:15 AM), a sprint progress report (11:30 AM), and an EOD reminder (6:00 PM).

---

## Technical Challenges & Fixes

### Challenge 1: Teams 403 Error on Sending Messages
**Problem:** The app was using an app-only (client credentials) token to send messages to the Teams chat. Microsoft blocks this unless the app is installed as a bot in the chat — which requires a Teams admin and Azure bot registration.

**Solution:** Implemented a delegated OAuth2 flow. A one-time login at `/api/login` authenticates as a real user (Yash). Messages are then sent on behalf of that user. The refresh token is stored server-side and auto-renewed, so login only needs to happen once.

---

### Challenge 2: Graph API Subscription Timing
**Problem:** The app was auto-subscribing to the Teams chat on startup. But Graph API validates the webhook by sending a GET request immediately — and the server wasn't ready yet, causing a BadGateway error. The subscription was never created.

**Solution:** Removed auto-subscribe on startup entirely. Subscription is now created manually via `POST /api/subscribe` after the server is confirmed healthy.

---

### Challenge 3: 504 Gateway Timeout on Large EODs
**Problem:** Each row insert required 5 sequential Microsoft Graph API calls (insert, fill color, font, alignment, write values). With 8 tasks at ~4 seconds per call, the total time was ~160 seconds — exceeding nginx's 60-second proxy timeout.

**Solution:**
- After the insert call (must be sequential), all formatting and value-write calls are now fired in parallel using `asyncio.gather`. This reduced per-row time from ~20s to ~8s.
- nginx `proxy_read_timeout` was increased to 120s as a safety buffer.
- End result: 8-task EOD now processes in ~35 seconds.

---

### Challenge 4: Blank Rows Appearing Between Entries
**Problem:** Microsoft Graph's `usedRange` API returns all rows that have any formatting (borders, fill colors, etc.) — even if those rows contain no data. A sheet might have data up to row 46, but rows 47–49 had formatting applied, so `len(values)` returned 49. New tasks were being inserted at row 50, leaving 3 blank rows.

**Solution:** Added `_last_data_row()` which scans backwards from the end of the returned rows to find the last row with actual non-empty cell content, skipping trailing blank/formatted rows.

---

### Challenge 5: FALSE/FALSE/TRUE Displaying as Text
**Problem:** WIP, Sent for Approval, and Closed columns in the Excel sheet use checkbox formatting. When the app wrote Python `False` values to inactive columns, Excel displayed them as the literal text "FALSE" instead of unchecked checkboxes.

**Solution:** Changed inactive stage columns to write `""` (empty string) instead of `False`. Empty = unchecked, which matches how the existing rows look. Only the active stage column writes `True`.

---

### Challenge 6: Brand Grouping Was Incorrect
**Problem:** The brand grouping logic tracked each brand's last row position in a dictionary. After inserting a row at position X, all rows at or after X shift down by 1 — but only the current brand's position was being updated. Other brands' positions went stale, causing subsequent tasks to be inserted at wrong rows.

**Solution:** After every insert, loop through all brands in the position map and increment any that are at or after the insert position. Also update `end_of_sheet` the same way.

---

### Challenge 7: AI Corrupting Comments
**Problem:** The AI enrichment step was allowed to set the `comments` field. For a task like "On-screen text for 2 videos", the AI wrote "Quantity: 2" as a comment, overriding the local parser's correctly extracted notes.

**Solution:** Removed `comments` from the AI enrichment field list. AI now only enriches: brand, activity_type, stage, dependency.

---

### Challenge 8: Backlog Promotion Always Reporting 0 Matches
**Problem:** The fuzzy-match threshold for backlog promotion was set at 0.80 (80% similarity). Most real-world EOD task names don't match backlog item names with that precision (e.g. "Wems video edit" vs "WEMS VIDEO EDIT 01").

**Solution:** Lowered threshold to 0.65. Added logging to confirm matches. Tested across multiple members — now correctly promoting items like "Wems video edit" → "WEMS VIDEO EDIT 01".

---

### Challenge 9: Prince's Sheet Showing 0 Tasks
**Problem:** Prince's sheet had all tasks in the Backlog column, not Sprint Backlog. The system only read rows where `sprint_backlog` was non-empty, so Prince's sheet appeared empty to the progress report.

**Solution:** Added a fallback in `_extract_existing_rows`: if `sprint_backlog` is empty but `backlog` has a value, use the backlog value as the task name.

---

### Challenge 11: Dual Chat Causing Split Traffic

**Problem:** The system had two separate chat IDs — `CHAT_ID` (original EOD chat) and `AGILE_CHAT_ID` (agile group chat). EOD messages were being listened to on the old `CHAT_ID`, while notifications were sent to `AGILE_CHAT_ID`. This meant the subscription was watching the wrong chat and EODs sent in the agile chat were silently ignored.

**Solution:** Consolidated everything to `AGILE_CHAT_ID`. The Graph API subscription now listens to the agile chat, `_send_teams_message` defaults to `AGILE_CHAT_ID`, and the fallback to `CHAT_ID` was removed. All EOD capture and all outgoing notifications now flow through a single chat.

---

### Challenge 10: Wrong Drive ID After Sheet Migration
**Problem:** The agile sheet was moved from one OneDrive to another (Dhwani's personal OneDrive). The new Drive ID was resolved correctly using the Graph API sharing URL decoder, but was accidentally saved with the last character truncated (`...mS1v` instead of `...mS1vX`), causing all sheet operations to return 400 Bad Request.

**Solution:** Corrected the Drive ID in `.env` on the server using `sed`.

---

## Improvements Made Over Time

| Improvement | Impact |
|-------------|--------|
| Switched from `usedRange` to `usedRange(valuesOnly=true)` | Prevented 1M-row reads on formatted sheets |
| 3-tier parser (Gemini → Groq → local) | 100% uptime even if AI APIs are down |
| Hybrid parsing (local structure + AI enrichment) | Faster, cheaper, more accurate than pure AI |
| Off-day scheduler | No notifications on Sundays, 1st Saturday, 3rd Saturday |
| Dedup cache for Graph notifications | Prevents same EOD being processed twice |
| Brand grouping with position tracking | Keeps sheet organized without manual sorting |
| `/backlog` Teams command | Members can add backlog items without opening Excel |
| Sprint progress report | Real-time SP tracking across the whole team |
| Self-loop prevention | Bot doesn't process its own messages as EODs |
| Single chat consolidation | All EOD listening and notifications unified to `AGILE_CHAT_ID` |

---

## Current Limitations

### 1. Story Points Are Not Auto-Filled
Story points (expected and actual) are intentionally left blank by the bot — team members fill these manually. This means the progress report is only as accurate as the team's discipline in updating them.

### 2. Shaily's Backlog Column Is Empty
Shaily's tasks are all directly in Sprint Backlog with no Backlog column entries. This means the backlog promotion flow doesn't apply — every EOD task from Shaily creates a new row rather than updating an existing one. Her backlog column needs to be populated manually.

### 3. No Duplicate Task Detection Across Sprint Backlogs
If the same task is mentioned in two EODs (e.g. "May calendar" on Wednesday and Thursday), a second row is created. The dedup against Sprint Backlog rows was intentionally disabled to avoid false merges. This means the team may see duplicate entries if the same task name is used repeatedly.

### 4. One Subscription at a Time
Microsoft Graph limits Teams chat subscriptions to 1 per resource per app. If the server restarts without deleting the old subscription first, the new subscribe call fails with "reached limit of 1 subscription". This requires manual cleanup via Graph API.

### 5. 35-Second Processing Time
Even with parallelization, each EOD takes 30–60 seconds due to multiple Graph API round trips. There is no immediate feedback to the user that their EOD was received (other than checking the sheet).

### 6. Login Required After Token Expiry
The delegated OAuth refresh token is valid for 90 days. After that, someone needs to re-login via the browser. There is no automatic alert when this happens.

---

## Recommendations

### Short-Term

1. **Add a Teams reply confirmation** — After processing an EOD, reply in the chat with a summary: "Got it Shaily — 7 tasks logged (5 Closed, 2 WIP)". This gives instant feedback.

2. **Add a `/status` command** — Members can type `/status` to see their current sprint tasks and progress without opening the sheet.

3. **Populate Shaily's backlog column** — Manually add her pending tasks to the Backlog column so the promotion flow works for her too.

4. **Alert on delegated token expiry** — Check token expiry on startup and log a warning (or Teams message) if it's within 7 days.

### Medium-Term

5. **Batch Graph API writes** — Instead of one API call per cell/format, use Excel session-based batch requests to write all rows in a single API call. This could reduce per-EOD time from 35s to under 10s.

6. **Add a `/sprint` command** — Shows the current sprint end date and total team progress in one message.

7. **Story point auto-suggestion** — After writing tasks, send a private message suggesting story points based on task complexity keywords (already computed by local parser but not sent to Teams).

8. **Handle subscription expiry gracefully** — Instead of just renewing, detect if renewal fails and automatically re-subscribe rather than requiring manual intervention.

### Long-Term

9. **Web dashboard** — A simple read-only dashboard at `agile-copilot.duckdns.org` showing each member's sprint progress, task counts, and story point burndown.

10. **Multi-chat support** — Currently monitors one Teams chat. Could be extended to support multiple chats/teams with different Excel workbooks.

11. **Sprint retrospective report** — At end of each sprint (15th and end of month), automatically generate and send a retrospective summary: tasks completed, story points delivered vs planned, members who didn't submit EODs.

---

## Results

| Metric | Before | After |
|--------|--------|-------|
| Sheet update frequency | Sporadic, often skipped | Every EOD automatically |
| Time spent updating sheet | 5–10 min/person/day | 0 (bot handles it) |
| Brand grouping | Manual, inconsistent | Automatic |
| Backlog visibility | Static, never updated | Promoted on first EOD mention |
| Sprint progress visibility | End of sprint only | Real-time (11:30 AM daily) |
| Task stage tracking | Manually marked | Auto-detected from EOD language |

---

*Report last updated: April 2026*
*Project: Agile Copilot v1.1*
*Author: World Goods Market Limited — Tech Team*
