# OutlookScheduler — Developer Reference

Personal cold-email campaign manager for Aryan Patel (CS junior, Collin College) to schedule and track professor outreach emails via Apple Mail on macOS. One user, solo use, no multi-tenancy.

## Running the app

```bash
python3 web_app.py
# Serves at http://127.0.0.1:5001
```

If port 5001 is already in use (old process still running):
```bash
lsof -ti :5001 | xargs kill -9; python3 web_app.py
```

Dependencies: `pip install flask apscheduler pandas` (see `requirements.txt`).

---

## Architecture

Two files do everything:

| File | Role |
|---|---|
| `web_app.py` | Flask backend, SQLite DB, APScheduler, AppleScript sending, ~1455 lines |
| `templates/index.html` | Entire frontend — HTML, CSS, vanilla JS in one file, ~2167 lines |

No build step. No npm. No TypeScript. No external JS libraries. Vanilla everything.

The backend is **never restarted for frontend-only changes** — just hard-refresh the browser. Backend changes require a restart.

---

## Backend (`web_app.py`)

### Database

SQLite at `./scheduler.db`. WAL mode enabled. All queries use `get_db()` context manager which commits on success and rolls back on exception.

**Tables:**

```
batches
  id, name, notes, professor_mode (unused), account_email, created_at

emails
  id, batch_id (FK→batches, CASCADE DELETE),
  to_addr, cc, bcc, subject, body, attachments, professor_link,
  send_time (ISO string), send_time_str (display string),
  status, manually_edited, error, response_status,
  duplicate_warning (0=none, 1=active, 2=dismissed),
  notes, created_at, sent_at

email_history
  id, email_id (FK→emails, CASCADE), changed_at, change_type, summary, old_values (JSON), new_values (JSON)

sent_address_log
  email_addr (UNIQUE COLLATE NOCASE), first_sent_at, send_count
  — written on every successful send; used for duplicate detection

processed_bounces
  message_id (PK), processed_at
  — Apple Mail message IDs already inspected for bounces; prevents reprocessing

profiles
  id, name, account_email, attachments, signature, cc_default, bcc_default, professor_mode, notes, created_at
  — NOTE: profiles feature was removed from the frontend UI but the backend routes and DB table still exist
```

**Email status values** (the only valid values — do not invent new ones):

| Status | Meaning |
|---|---|
| `needs_review` | Default on import; must be approved before scheduling |
| `verified` | Approved in review screen; eligible for scheduling |
| `pending` | Has a send time set, not yet scheduled with APScheduler |
| `scheduled` | APScheduler job registered, waiting to fire |
| `overdue` | Had a send time that passed without being sent |
| `sending` | `_dispatch` has started, in-flight |
| `sent` | Successfully sent |
| `failed` | Send attempt threw an exception, or bounce received |
| `skipped` | Manually skipped |
| `invalid` | CSV row had an unparseable send_time |

**`response_status`** (separate from status, tracks professor reply):
`none` | `replied` | `interested` | `not_interested` | `bounced`

### Sending pipeline

All sending goes through `_dispatch(email_id)`:

1. Atomically sets status to `'sending'` (guards: won't send if already `sent`, `sending`, `skipped`, or `needs_review`)
2. Resolves which sender account to use: batch's `account_email` → global `selected_account_email` → first Apple Mail account
3. Chooses send method:
   - If SMTP password stored in macOS Keychain for that account → `send_via_smtp()` (Office365 SMTP, port 587)
   - Else if Apple Mail accounts found → `send_via_mail()` (AppleScript → Apple Mail)
   - Else → `send_via_outlook()` (AppleScript → Microsoft Outlook)
4. On success: sets status `'sent'`, records `sent_at`, upserts `sent_address_log`
5. On failure: sets status `'failed'`, writes error message

**AppleScript sending (`send_via_mail`)**: Writes body to a temp file (avoids quote escaping limits), builds an AppleScript that creates and sends an outgoing message in Apple Mail. Body newlines are converted `\n → \r` because AppleScript/Mail expects CR.

**AppleScript sending (`send_via_outlook`)**: Same approach but targets Microsoft Outlook. Compiles to a `.scpt` file first via `osacompile` because Outlook scripts are too large to pass via `-e`.

**SMTP sending (`send_via_smtp`)**: Uses `smtplib` with Office365 SMTP. Password stored/retrieved from macOS Keychain under service name `"EmailScheduler"`.

### Scheduling

Two scheduling paths:

**`POST /api/schedule`** — Schedule all pending/overdue/verified emails that have a `send_time` set. Emails with past send times are dispatched immediately in threads. Future emails get APScheduler `date` trigger jobs with id `email_{id}`.

**`POST /api/smart-schedule`** — Generates random timestamps across a date range and assigns them. Supports business-hours mode (configurable start hour, end hour, days of week). Optional minimum gap between sends. Shuffles email order before assigning times.

**Restart recovery** (`_restore_scheduled_jobs`): Called at startup. Reads all emails with `status='scheduled'`, re-registers their APScheduler jobs. Emails whose send time has already passed get set to `'overdue'` instead. This is critical — APScheduler is in-memory only and loses all jobs on restart.

### Bounce detection

`_check_bounces()` runs every 5 minutes via APScheduler and also on-demand via `POST /api/check-bounces`.

**Algorithm (two-pass to avoid reading every email body):**

Pass 1: AppleScript reads `(message id, subject, sender)` of ALL messages in INBOX across every account. Uses tab-separated, newline-per-record output (not AppleScript list return, which gets comma-joined and breaks on email content).

Filter: keeps only messages whose subject matches `_BOUNCE_SUBJECTS` or sender matches `_BOUNCE_SENDERS` (e.g. "mailer-daemon", "undeliverable", "delivery has failed").

Pass 2: For each bounce candidate not already in `processed_bounces`, fetches the message body via a separate AppleScript call targeting that specific `message id`.

Address extraction: regex scans body for email addresses, filters out system addresses (mailer-daemon, postmaster, Microsoft domains). First remaining address is the failed recipient.

DB update: Finds the most recent `sent` email with that `to_addr`, sets its status to `'failed'` with error message. Writes to `processed_bounces` regardless of whether a match was found (prevents re-scanning).

Marks bounce email as read in Apple Mail.

### Duplicate detection

`_check_duplicate(conn, to_addr)` returns True if:
- The address exists in `sent_address_log` (already sent to this person ever), OR
- Another non-terminal email exists with the same address (status not in `skipped`, `failed`, `invalid`, `sent`)

On import, duplicates get `duplicate_warning=1`. The Warnings filter tab shows these. Users can dismiss per-email (`duplicate_warning=2`).

### CSV import

`POST /api/upload` accepts a CSV with required columns `to`, `subject`, `body` and optional `send_time`, `professor_link`, `cc`, `bcc`, `attachments`.

`\n` escape sequences in the body field are converted to real newlines (AI-generated CSVs use `\n\n` for paragraph breaks).

All imported emails start as `needs_review` regardless of what status the CSV might imply.

If no `batch_id` is provided, a new batch is created automatically named `"Import MMM DD HH:MM"`.

### All API routes

```
GET  /                          — serves index.html

GET  /api/batches               — list all batches with aggregate counts
POST /api/batches               — create batch {name, notes, account_email}
GET  /api/batches/:id           — get single batch with counts
PATCH /api/batches/:id          — update {name, notes, account_email, professor_mode}
DELETE /api/batches/:id         — delete batch and all its emails

GET  /api/emails                — list emails, params: page, per_page, status, batch_id, duplicate_warning
POST /api/emails                — create single email
GET  /api/emails/:id            — get email + history
PATCH /api/emails/:id           — edit fields + send_time
DELETE /api/emails/:id          — delete email
PATCH /api/emails/:id/status    — set status (allowed: pending/overdue/skipped/failed/needs_review/verified)
POST /api/emails/:id/verify     — set status=verified
POST /api/emails/:id/unverify   — set status=needs_review
POST /api/emails/:id/send-now   — immediate send in background thread
PATCH /api/emails/:id/response  — set response_status

GET  /api/stats                 — counts by status + warnings, optional ?batch_id=
GET  /api/warnings              — duplicate and failure details, optional ?batch_id=

POST /api/bulk-verify           — {ids:[]} → set needs_review→verified
POST /api/bulk-skip             — {ids:[]} → set status=skipped (not sent/sending)
POST /api/bulk-unskip           — {ids:[]} → restore skipped→pending/overdue
POST /api/bulk-delete           — {ids:[]} → delete, removes APScheduler jobs
POST /api/bulk-set-time         — {ids:[], send_time:"ISO"} → set absolute send time
POST /api/bulk-move-batch       — {ids:[], batch_id:N} → move to batch (not sent/sending)
POST /api/bulk-attach           — multipart file + optional batch_id → attach to all unsent
POST /api/bulk-clear-attachments — {batch_id?} → clear attachments from all unsent
POST /api/bulk-shift-time       — {delta_minutes:N, batch_id?} → shift all scheduled times

POST /api/schedule              — register APScheduler jobs for all pending/overdue/verified
POST /api/smart-schedule        — {start, end, business_hours, biz_start_h, biz_end_h, biz_days, min_gap, batch_id?}

GET  /api/accounts              — list Apple Mail accounts + selected account
POST /api/set-account           — {email} → set global sending account
POST /api/smtp-password         — {email, password} → store/clear in Keychain
GET  /api/smtp-status           — check if SMTP password configured

POST /api/upload                — CSV import, multipart: file + optional batch_id/batch_name
GET  /api/sample-csv            — download example CSV

POST /api/check-bounces         — trigger bounce scan immediately
POST /api/reset                 — delete all data (or just one batch if batch_id provided)

GET  /api/profiles/:id and CRUD  — profile backend exists but profiles feature removed from UI
```

---

## Frontend (`templates/index.html`)

Single-file: all HTML, CSS, and JS. No framework. No build. ~2167 lines.

### Design system

OKLCH color tokens in `:root`. Key values:
```css
--sidebar-bg: oklch(0.20 0.025 220)   /* dark slate-teal sidebar */
--primary: oklch(0.62 0.15 195)       /* teal accent */
--accent: oklch(0.65 0.18 50)         /* orange, used for warnings/urgent */
--bg: oklch(0.99 0.003 220)           /* near-white main content */
--sidebar: 240px                      /* sidebar width */
```

Layout: dark sidebar (240px) + white main content (Superhuman pattern). White topbar inside main with sticky positioning.

### Central state object `S`

All app state lives in one global object:
```js
const S = {
  activeBatch: '',        // string ID of selected batch, '' = all
  batches: [],            // array of batch objects from API
  page: 1,
  perPage: 50,
  pages: 1,
  total: 0,
  filter: '',             // current status filter tab value
  tableEmails: [],        // emails currently rendered in table
  selected: new Set(),    // Set of selected email IDs (numbers)
  stats: {},              // counts from /api/stats
  poll: null,             // setInterval handle
  detailId: null,         // email ID open in detail panel
  detailEmail: null,
  composeId: null,        // null=new, number=editing existing
  batchEditId: null,
  detailTab: 'info',
  review: { emails:[], idx:0, batchId:null, editing:false },
  accounts: [],
  warnings: { duplicates:[], failures:[] },
  warnDismissed: false,
  warnPanelOpen: true,
};
```

### Key JS functions

**Data fetching:**
- `fetchBatches()` — GET /api/batches, populates sidebar and `S.batches`
- `fetchTable()` — GET /api/emails with current filters/pagination, renders table
- `fetchStats()` — GET /api/stats, updates tab badges and attention bar
- `loadAccounts()` — GET /api/accounts, populates account selector dropdown

**Rendering:**
- `renderSidebar()` — rebuilds batch list in sidebar
- `renderTable()` — renders tbody rows from `S.tableEmails`
- `renderAttentionBar(d)` — shows orange/amber/red rows for needs_review/overdue/failed
- `renderReviewCard()` — renders current email in review screen

**Navigation:**
- `selectBatch(bid)` — switches active batch, resets filter, fetches table
- `setFilter(status)` — sets `S.filter`, resets to page 1, fetches table
- `updateFromLabel(b)` — updates "sending from" label in topbar

**Review screen:**
- `openReview(batchId)` — loads needs_review emails, opens fullscreen review overlay
- `reviewNext()` — approves current email (POST /verify), advances to next
- `reviewBack()` — goes to previous email
- `reviewSkip()` — skips current email (POST /status → skipped), advances
- `closeReview()` — closes review screen, refreshes table

**Keyboard shortcuts (only active when review screen is open):**
- `Enter` or `→` — approve and advance
- `←` — go back
- `S` — skip

**Bulk actions (selection bar):**
- `bulkApproveSelected()` — POST /api/bulk-verify
- `sendSelected()` — POST /api/emails/:id/send-now for each selected
- `bulkSetTimeSelected()` — opens Set Time modal
- `bulkMoveBatchSelected()` — opens Move to Batch modal
- `bulkSkipSelected()` — POST /api/bulk-skip
- `bulkUnskipSelected()` — POST /api/bulk-unskip
- `bulkDeleteSelected()` — POST /api/bulk-delete (confirms first)
- `clearSel()` — clears selection, hides bar

**Scheduling:**
- `scheduleAll()` — POST /api/schedule for active batch
- `openSmartSchedule()` — opens Smart Schedule modal
- `submitSmartSchedule()` — reads form, reads localStorage for biz hours defaults, POST /api/smart-schedule

**Business hours** are saved to `localStorage` key `biz_hours_prefs` as `{start:"HH:MM", end:"HH:MM", days:[0,1,2,3,4]}`. Read on modal open, saved via `saveBizDefaults()`.

**Modals:** `openModal(id)` / `closeModal(id)` toggle `.overlay` visibility. All modals are `<div class="overlay" id="m-*">` elements.

### Filter tabs

Filter tabs use `data-s` attribute. Values map directly to status strings. Special cases:
- `data-s=""` = All
- `data-s="warnings"` = fetches with `&duplicate_warning=1`

Tab badges (`id="tb-{status}"`) are updated by `fetchStats()`.

### Selection bar

Appears when `S.selected.size > 0`. Styled to match sidebar (dark background). Contains all selection-scoped actions inline. Batch-wide actions (attach, clear attach, shift times, delete all data) remain in the Tools dropdown.

### Attention bar

`renderAttentionBar(d)` renders rows with colored backgrounds:
- Orange (`attn-row-review`) for `needs_review > 0`
- Amber (`attn-row-overdue`) for `overdue > 0`
- Red (`attn-row-failed`) for `failed > 0`

"Review now →" calls `openReview(S.activeBatch)` directly.

### Polling

`setInterval` runs every 15 seconds calling `fetchTable()`, `fetchStats()`, `fetchBatches()`. Stored in `S.poll`.

---

## Common tasks for an AI

### Adding a new bulk action

1. Add backend route in `web_app.py` as `POST /api/bulk-something`
2. Add a `btn-sel` button in the `<!-- Selection bar -->` HTML block
3. Add the JS function near `bulkApproveSelected` etc.
4. If it needs a modal, add an `<div class="overlay" id="m-something">` block near the other bulk modals

### Adding a new email status

1. Add it to `_dispatch`'s exclusion guard if it should block sending
2. Add it to `SLABELS` map in JS: `{ needs_review:'Needs Review', ... }`
3. Add a `.badge-{status}` CSS rule
4. Add it to `SICONS` if it should have an icon
5. Add a filter tab if needed

### Changing the send time logic

`_business_timestamps()` generates timestamps. Takes `start_dt`, `end_dt`, `count`, `biz_start_h`, `biz_end_h`, `biz_days` (list of weekday ints, 0=Mon). Returns a sorted list of `datetime` objects.

### Modifying what gets scheduled

`schedule_all` and `smart_schedule` both filter: `WHERE status IN ('pending','overdue','verified')`. `needs_review` emails are intentionally excluded — they must be approved first.

### Debugging AppleScript issues

All AppleScript is run via `subprocess.run(["osascript", "-e", script])`. Errors come back in `r.stderr`. For complex scripts (Outlook), it compiles first with `osacompile`. Check `r.returncode` and `r.stderr` for failures.

### Debugging bounce detection

The bounce checker writes to stdout: `[bounce] Marked email {id} ({addr}) as failed`. Check server terminal output. The `processed_bounces` table records all inspected message IDs — clear it if you need to re-scan already-seen messages:
```sql
DELETE FROM processed_bounces;
```

---

## What NOT to do

- **Do not modify `web_app.py` for cosmetic/UI reasons** — backend only.
- **Do not add a build step, npm, or framework** — frontend stays vanilla.
- **Do not introduce `str | None` type hints** — Python version is pre-3.10, use no annotation or `Optional[str]` from `typing`.
- **Do not add a new status value** without updating `_dispatch`'s guard, `SLABELS` in JS, and a badge CSS rule.
- **Do not schedule `needs_review` emails** — they must go through the review/verify flow first.
- **Do not use `first account`** in AppleScript — always iterate `every account` to cover all Mail accounts.
- **The `profiles` table and backend routes still exist** but the profiles feature was fully removed from the frontend. Don't re-add profile UI without being asked.
