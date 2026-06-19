# Email Scheduler

A personal cold-email campaign manager for macOS. Import a CSV of recipients,
review and verify each message, schedule them across realistic business hours,
and send them through **Apple Mail**, **Microsoft Outlook**, or **SMTP** — all
from a clean local web interface. It also tracks replies and automatically
detects bounces.

Built for one-person outreach campaigns, not bulk
marketing. Everything runs locally on your Mac; no data leaves your machine.

---

## Requirements

- **macOS** (sending uses AppleScript to drive Apple Mail / Outlook)
- **Python 3** — check with `python3 --version`. If missing, install from
  [python.org](https://www.python.org/downloads/) or run `brew install python`.
- **Apple Mail or Microsoft Outlook** set up with the account you want to send
  from. (Alternatively, configure SMTP — see below.)

---

## Quick start

1. Download or clone this repository.
2. **Double-click `EmailScheduler.command`** in Finder.

That's it. On first launch it sets up everything automatically (this takes about
a minute), then opens the app in your browser at <http://127.0.0.1:5001>.

> The first time you open a `.command` file, macOS may warn that it's from an
> unidentified developer. Right-click the file → **Open** → **Open** to allow it.

Prefer the terminal? Run the same launcher there:

```bash
./run_web.sh
```

To stop the app, click **Stop App** in the top-right of the interface, press
**Ctrl+C** in its terminal window, or just close the window.

---

## How sending works

When you schedule an email, Email Scheduler picks a send method automatically:

1. **SMTP** — if you've stored an app password for the account (Office 365 SMTP).
2. **Apple Mail** — if Mail has accounts configured (default).
3. **Microsoft Outlook** — as a fallback.

Sending via Apple Mail or Outlook drives the app with AppleScript, so the
corresponding app must be installed and signed in. The first send may trigger a
macOS prompt asking to allow automation — click **OK**.

### Using SMTP instead

In the app, open the account settings and store an app password for your
address. Passwords are kept in the **macOS Keychain** (never written to disk or
committed). Once set, that account sends over SMTP (Office 365, port 587).

---

## Importing recipients

Upload a CSV with these columns:

| Column | Required | Notes |
|---|---|---|
| `to` | yes | recipient email address |
| `subject` | yes | |
| `body` | yes | `\n\n` is converted to paragraph breaks |
| `send_time` | no | ISO timestamp; otherwise schedule it in-app |
| `cc`, `bcc` | no | comma-separated |
| `attachments` | no | file path(s) |
| `professor_link` | no | a link to verify the recipient before sending |

Grab a working template from **Download sample CSV** in the app, or the
`GET /api/sample-csv` endpoint.

All imported emails start as **Needs Review** — you approve them in the review
screen before they can be scheduled. This is a deliberate safety net against
sending to a wrong or hallucinated address.

---

## The workflow

```
Import CSV  →  Review & Verify  →  Schedule  →  Send  →  Track replies / bounces
```

- **Review** every message one at a time (keyboard: Enter to approve, ← back, S to skip).
- **Smart Schedule** spreads sends across a date range within business hours,
  with an optional minimum gap between emails.
- **Duplicate detection** warns you if you've already emailed an address.
- **Bounce detection** scans your inbox every few minutes and flags failures.

---

## Data & privacy

- All data lives in a local SQLite file (`scheduler.db`) in this folder.
- The database, your uploads, and the virtual environment are **git-ignored** —
  they are never committed or pushed.
- SMTP passwords live only in the macOS Keychain.

To wipe everything and start fresh, use **Reset** in the app, or delete
`scheduler.db*`.

---

## Project layout

| Path | What it is |
|---|---|
| `web_app.py` | Flask backend: database, scheduler, sending, bounce detection |
| `templates/index.html` | The entire frontend (HTML/CSS/JS, no build step) |
| `run_web.sh` | Launcher: sets up the venv, installs deps, starts the server |
| `EmailScheduler.command` | Double-click launcher for Finder |
| `requirements.txt` | Python dependencies |

No build step, no npm, no framework — just Python and a single HTML file.

---

## Troubleshooting

- **"Port 5001 in use"** — the launcher clears it automatically. To do it
  manually: `lsof -ti :5001 | xargs kill -9`.
- **Emails don't send** — make sure Apple Mail or Outlook is open and signed in,
  and that you allowed the automation prompt. Check the launcher window for
  errors.
- **Reset everything** — delete `scheduler.db`, `scheduler.db-shm`, and
  `scheduler.db-wal`, then relaunch.
