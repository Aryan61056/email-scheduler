#!/usr/bin/env python3
"""Email Scheduler — v3 (Flask + SQLite + Apple Mail)"""

import html as _html
import io
import json
import os
import random
import re
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time as _time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, send_file

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "scheduler.db")
UPLOADS_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

scheduler = BackgroundScheduler(daemon=True)
scheduler.start()

selected_account_email: str = ""

# ── Update checking ──────────────────────────────────────────────────────────────
# The app self-updates from GitHub: it compares the local git commit against the
# latest commit on the tracked branch and offers a one-click `git pull` + restart.
GITHUB_REPO = "Aryan61056/email-scheduler"
GITHUB_BRANCH = "main"
_UPDATE_CACHE_TTL = 1800  # seconds — don't hit the GitHub API more than ~twice/hour
_update_state = {"checked_at": 0.0, "data": None}
_update_lock = threading.Lock()


def _git(*args):
    """Run a git command inside the app dir; return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(["git", "-C", APP_DIR, *args],
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def _is_git_repo():
    return os.path.isdir(os.path.join(APP_DIR, ".git"))


def _local_commit():
    code, out, _ = _git("rev-parse", "HEAD")
    return out if code == 0 and out else None


def _fetch_latest_commit():
    """Latest commit on the tracked branch, via the public GitHub API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "EmailScheduler-update-check",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode())
    commit = payload.get("commit", {}) or {}
    return {
        "sha": payload.get("sha", "") or "",
        "message": (commit.get("message", "") or "").split("\n")[0][:140],
        "date": (commit.get("author", {}) or {}).get("date", ""),
        "url": payload.get("html_url") or f"https://github.com/{GITHUB_REPO}",
    }


def _compute_update(force=False):
    """Build the version/update payload, caching the GitHub lookup for a while."""
    with _update_lock:
        now = _time.time()
        cached = _update_state["data"]
        if (not force and cached is not None
                and now - _update_state["checked_at"] < _UPDATE_CACHE_TTL):
            return cached
        local = _local_commit()
        data = {
            "git": _is_git_repo(),
            "current": local,
            "current_short": local[:7] if local else None,
            "latest": None, "latest_short": None,
            "latest_message": None, "latest_date": None,
            "latest_url": f"https://github.com/{GITHUB_REPO}",
            "update_available": False,
            "checked_at": datetime.now().isoformat(),
            "error": None,
        }
        if not data["git"]:
            data["error"] = "Not a git checkout — updates are managed manually."
            _update_state.update(checked_at=now, data=data)
            return data
        try:
            latest = _fetch_latest_commit()
            data["latest"] = latest["sha"]
            data["latest_short"] = latest["sha"][:7] if latest["sha"] else None
            data["latest_message"] = latest["message"]
            data["latest_date"] = latest["date"]
            data["latest_url"] = latest["url"]
            if local and latest["sha"]:
                data["update_available"] = local != latest["sha"]
        except urllib.error.HTTPError as exc:
            data["error"] = f"GitHub returned HTTP {exc.code}"
        except Exception as exc:
            data["error"] = f"Could not reach GitHub: {exc}"
        _update_state.update(checked_at=now, data=data)
        return data


def _restart_process(install_deps=False):
    """Re-exec the server in place so freshly pulled code takes effect."""
    def _do():
        _time.sleep(0.6)
        if install_deps:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r",
                                os.path.join(APP_DIR, "requirements.txt")],
                               capture_output=True, text=True, timeout=300)
            except Exception:
                pass
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        os.chdir(APP_DIR)
        os.execv(sys.executable, [sys.executable, os.path.join(APP_DIR, "web_app.py")])
    threading.Thread(target=_do, daemon=True).start()


# ── Database ───────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                notes           TEXT DEFAULT '',
                professor_mode  INTEGER DEFAULT 0,
                account_email   TEXT DEFAULT '',
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profiles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                account_email   TEXT DEFAULT '',
                attachments     TEXT DEFAULT '',
                signature       TEXT DEFAULT '',
                cc_default      TEXT DEFAULT '',
                bcc_default     TEXT DEFAULT '',
                professor_mode  INTEGER DEFAULT 0,
                notes           TEXT DEFAULT '',
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS emails (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id          INTEGER REFERENCES batches(id) ON DELETE CASCADE,
                to_addr           TEXT NOT NULL DEFAULT '',
                cc                TEXT DEFAULT '',
                bcc               TEXT DEFAULT '',
                subject           TEXT DEFAULT '',
                body              TEXT DEFAULT '',
                attachments       TEXT DEFAULT '',
                send_time         TEXT,
                send_time_str     TEXT DEFAULT '',
                status            TEXT DEFAULT 'pending',
                manually_edited   INTEGER DEFAULT 0,
                error             TEXT,
                response_status   TEXT DEFAULT 'none',
                duplicate_warning INTEGER DEFAULT 0,
                notes             TEXT DEFAULT '',
                created_at        TEXT NOT NULL,
                sent_at           TEXT,
                content_warning   INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS email_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id    INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
                changed_at  TEXT NOT NULL,
                change_type TEXT NOT NULL,
                summary     TEXT NOT NULL,
                old_values  TEXT DEFAULT '{}',
                new_values  TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS sent_address_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email_addr    TEXT NOT NULL UNIQUE COLLATE NOCASE,
                first_sent_at TEXT NOT NULL,
                send_count    INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS processed_bounces (
                message_id  TEXT NOT NULL PRIMARY KEY,
                processed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Migrate existing databases
        for stmt in [
            "ALTER TABLE batches ADD COLUMN professor_mode INTEGER DEFAULT 0",
            "ALTER TABLE batches ADD COLUMN account_email TEXT DEFAULT ''",
            "ALTER TABLE profiles ADD COLUMN professor_mode INTEGER DEFAULT 0",
            "ALTER TABLE emails ADD COLUMN duplicate_warning INTEGER DEFAULT 0",
            "ALTER TABLE emails ADD COLUMN content_warning INTEGER DEFAULT 0",
            "ALTER TABLE emails ADD COLUMN reply_body TEXT DEFAULT ''",
            "ALTER TABLE emails ADD COLUMN reply_received_at TEXT",
            "ALTER TABLE emails ADD COLUMN reply_delta_seconds INTEGER",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        # The interested / not_interested response labels were removed — fold any
        # existing rows back into the generic 'replied' label so they aren't stranded.
        try:
            conn.execute("UPDATE emails SET response_status='replied' "
                         "WHERE response_status IN ('interested','not_interested')")
        except Exception:
            pass


init_db()

# ── Content-warning helpers ─────────────────────────────────────────────────────

_CONTENT_WARNING_PHRASES = ["not found in any format"]


def _has_content_warning(body: str, subject: str) -> bool:
    text = ((body or "") + " " + (subject or "")).lower()
    return any(p in text for p in _CONTENT_WARNING_PHRASES)


# Backfill: flag existing emails imported before this column existed.
with get_db() as _conn:
    _conn.execute(
        "UPDATE emails SET content_warning=1 "
        "WHERE content_warning=0 AND ("
        + " OR ".join(
            "LOWER(body) LIKE ? OR LOWER(subject) LIKE ?"
            for _ in _CONTENT_WARNING_PHRASES
        )
        + ")",
        [v for p in _CONTENT_WARNING_PHRASES for v in (f"%{p}%", f"%{p}%")]
    )


def row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    if "to_addr" in d:
        d["to"] = d.pop("to_addr")
    d["manually_edited"] = bool(d.get("manually_edited", 0))
    d["duplicate_warning"] = int(d.get("duplicate_warning", 0))
    return d


def _add_history(conn, email_id, change_type, summary, old_vals=None, new_vals=None):
    conn.execute(
        "INSERT INTO email_history (email_id, changed_at, change_type, summary, old_values, new_values) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (email_id, datetime.now().isoformat(), change_type, summary,
         json.dumps(old_vals or {}), json.dumps(new_vals or {}))
    )


def _check_duplicate(conn, to_addr, exclude_id=None):
    if not to_addr:
        return False
    if conn.execute(
        "SELECT 1 FROM sent_address_log WHERE email_addr=? COLLATE NOCASE", (to_addr,)
    ).fetchone():
        return True
    q = ("SELECT 1 FROM emails WHERE LOWER(to_addr)=LOWER(?) "
         "AND status NOT IN ('skipped','failed','invalid','sent')")
    args = [to_addr]
    if exclude_id:
        q += " AND id!=?"
        args.append(exclude_id)
    return bool(conn.execute(q, args).fetchone())


# ── AppleScript helpers ────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _parse_addrs(raw) -> list:
    if not raw:
        return []
    s = str(raw).strip()
    if s in ("", "nan"):
        return []
    return [a.strip() for a in s.split(",") if a.strip()]


def get_sending_accounts() -> list:
    script = (
        'tell application "Mail"\n'
        '  set out to {}\n'
        '  repeat with a in every account\n'
        '    set nm to name of a\n'
        '    set addrs to email addresses of a\n'
        '    repeat with e in addrs\n'
        '      set end of out to nm & "|||" & (e as string)\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=12)
    accounts = []
    seen = set()
    for item in r.stdout.strip().split(","):
        parts = item.strip().split("|||")
        if len(parts) >= 2 and parts[1].strip() and parts[1].strip() not in seen:
            email = parts[1].strip()
            seen.add(email)
            accounts.append({"name": parts[0].strip(), "email": email})
    return accounts


# ── Keychain helpers ───────────────────────────────────────────────────────────
_KC_SERVICE = "EmailScheduler"

def _kc_get(account: str):
    r = subprocess.run(
        ["security", "find-generic-password", "-s", _KC_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True
    )
    return r.stdout.strip() if r.returncode == 0 else None

def _kc_set(account: str, password: str):
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", _KC_SERVICE, "-a", account, "-w", password],
        capture_output=True, text=True
    )

def _kc_del(account: str):
    subprocess.run(
        ["security", "delete-generic-password", "-s", _KC_SERVICE, "-a", account],
        capture_output=True, text=True
    )


# ── Mail senders ───────────────────────────────────────────────────────────────

def get_mail_accounts():
    return [a["email"].lower() for a in get_sending_accounts()]


def send_via_mail(to, subject, body, cc="", bcc="", attachments="", sender_email=""):
    to_list = _parse_addrs(to)
    if not to_list:
        raise ValueError("No recipients specified")
    cc_list = _parse_addrs(cc)
    bcc_list = _parse_addrs(bcc)
    attach_list = [p for p in _parse_addrs(attachments) if os.path.exists(p)]
    body_cr = str(body).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(body_cr); tmp.close()
    try:
        parts = ['tell application "Mail"']
        parts.append(f'    set msgBody to read POSIX file "{_esc(tmp.name)}"')
        parts.append(f'    set msg to make new outgoing message with properties'
                     f' {{subject:"{_esc(str(subject))}", content:msgBody, visible:false}}')
        if sender_email:
            parts.append(f'    set sender of msg to "{_esc(sender_email)}"')
        parts.append('    tell msg')
        for addr in to_list:
            parts.append(f'        make new to recipient with properties {{address:"{_esc(addr)}"}}')
        for addr in cc_list:
            parts.append(f'        make new cc recipient with properties {{address:"{_esc(addr)}"}}')
        for addr in bcc_list:
            parts.append(f'        make new bcc recipient with properties {{address:"{_esc(addr)}"}}')
        for path in attach_list:
            parts.append(f'        make new attachment with properties'
                         f' {{file name:POSIX file "{_esc(path)}"}}')
        parts += ['    end tell', '    send msg', 'end tell']
        r = subprocess.run(["osascript", "-e", "\n".join(parts)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "Apple Mail AppleScript error")
    finally:
        try: os.unlink(tmp.name)
        except Exception: pass


def send_via_smtp(to, subject, body, cc="", bcc="", attachments="", sender_email=""):
    password = _kc_get(sender_email)
    if not password:
        raise ValueError(f"No SMTP password stored for {sender_email}.")
    body_text = str(body).replace("\r\n", "\n").replace("\r", "\n")
    if body_text.strip().lower().startswith(("<html", "<!doctype")):
        html_body = body_text
    else:
        lines = [_html.escape(ln) for ln in body_text.split("\n")]
        html_body = "<html><body>" + "<br>".join(lines) + "</body></html>"
    attach_list = [p for p in _parse_addrs(attachments) if os.path.exists(p)]
    outer = MIMEMultipart("mixed")
    outer["Subject"] = subject; outer["From"] = sender_email; outer["To"] = to
    if cc: outer["Cc"] = cc
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    outer.attach(alt)
    for path in attach_list:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read())
        part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
        outer.attach(part)
    recipients = _parse_addrs(to) + _parse_addrs(cc) + _parse_addrs(bcc)
    with smtplib.SMTP("smtp.office365.com", 587, timeout=30) as server:
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(sender_email, password)
        server.sendmail(sender_email, recipients, outer.as_string())


# ── Dispatch ───────────────────────────────────────────────────────────────────

def _dispatch(email_id: int):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE emails SET status='sending' WHERE id=? "
            "AND status NOT IN ('sent','sending','skipped','needs_review')",
            (email_id,)
        )
        if cur.rowcount == 0:
            return
        row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
        batch_account = ""
        if row["batch_id"]:
            br = conn.execute("SELECT account_email FROM batches WHERE id=?", (row["batch_id"],)).fetchone()
            if br:
                batch_account = (br["account_email"] or "").strip()
        _add_history(conn, email_id, "status", "Sending started")

    try:
        mail_accounts = get_mail_accounts()
        sender = batch_account or selected_account_email
        if sender.lower() not in [a.lower() for a in mail_accounts] and mail_accounts:
            sender = next(
                (a["email"] for a in get_sending_accounts() if a["email"].lower() == mail_accounts[0]),
                mail_accounts[0]
            )
        kwargs = dict(
            to=row["to_addr"], subject=row["subject"], body=row["body"],
            cc=row["cc"] or "", bcc=row["bcc"] or "",
            attachments=row["attachments"] or "", sender_email=sender,
        )
        if sender and _kc_get(sender):
            send_via_smtp(**kwargs)
        elif mail_accounts:
            send_via_mail(**kwargs)
        else:
            raise RuntimeError("No Apple Mail account found and no SMTP password "
                               "stored — cannot send. Set up Apple Mail or add an "
                               "SMTP app password in account settings.")

        now_iso = datetime.now().isoformat()
        with get_db() as conn:
            conn.execute("UPDATE emails SET status='sent', sent_at=? WHERE id=?", (now_iso, email_id))
            conn.execute(
                "INSERT INTO sent_address_log (email_addr, first_sent_at, send_count) VALUES (?,?,1) "
                "ON CONFLICT(email_addr) DO UPDATE SET send_count=send_count+1",
                (row["to_addr"], now_iso)
            )
            _add_history(conn, email_id, "status", "Email sent successfully")
    except Exception as exc:
        with get_db() as conn:
            conn.execute("UPDATE emails SET status='failed', error=? WHERE id=?", (str(exc), email_id))
            _add_history(conn, email_id, "status", f"Send failed: {exc}")


def _restore_scheduled_jobs():
    """Re-register APScheduler jobs for emails that were scheduled before a restart."""
    now = datetime.now()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, send_time FROM emails WHERE status='scheduled' AND send_time IS NOT NULL"
        ).fetchall()
        overdue_ids = []
        for row in rows:
            try:
                send_time = datetime.fromisoformat(row["send_time"])
            except Exception:
                continue
            if send_time <= now:
                overdue_ids.append(row["id"])
            else:
                scheduler.add_job(_dispatch, trigger="date", run_date=send_time,
                                  args=[row["id"]], id=f"email_{row['id']}", replace_existing=True)
        if overdue_ids:
            placeholders = ",".join("?" * len(overdue_ids))
            conn.execute(
                f"UPDATE emails SET status='overdue' WHERE id IN ({placeholders})",
                overdue_ids
            )
    restored = len(rows) - len(overdue_ids)
    if rows:
        print(f"  Restored {restored} scheduled job(s), marked {len(overdue_ids)} as overdue")


_restore_scheduled_jobs()


# ── Auto-reschedule ────────────────────────────────────────────────────────────

# Grace period: how long after send_time before we declare an email overdue.
# Gives APScheduler time to fire without us clobbering the job.
_OVERDUE_GRACE_SECONDS = 120  # 2 minutes


def _get_ar_settings() -> dict:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='auto_reschedule'").fetchone()
            if row:
                return json.loads(row["value"])
    except Exception:
        pass
    return {"enabled": False, "window_minutes": 60}


def _save_ar_settings(settings: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('auto_reschedule', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(settings),)
        )


def _get_reply_settings() -> dict:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='reply_check'").fetchone()
            if row:
                s = json.loads(row["value"])
                return {"enabled": bool(s.get("enabled", True)),
                        "interval_minutes": max(5, int(s.get("interval_minutes", 60)))}
    except Exception:
        pass
    return {"enabled": True, "interval_minutes": 60}


def _save_reply_settings(settings: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('reply_check', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(settings),)
        )


def _apply_reply_schedule():
    """(Re)register or remove the periodic reply checker to match saved settings."""
    s = _get_reply_settings()
    try:
        scheduler.remove_job("reply_checker")
    except Exception:
        pass
    if s.get("enabled", True):
        scheduler.add_job(
            _check_replies_job, trigger="interval", minutes=s["interval_minutes"],
            id="reply_checker", replace_existing=True, max_instances=1, coalesce=True,
            misfire_grace_time=120, next_run_time=datetime.now() + timedelta(seconds=45))


def _auto_reschedule_job():
    """Periodic job: mark overdue emails (with grace period), then reschedule if enabled."""
    now = datetime.now()
    cutoff = now - timedelta(seconds=_OVERDUE_GRACE_SECONDS)

    # Mark emails that missed their send window as overdue.
    # Only touch statuses that weren't already dispatched.
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM emails "
            "WHERE status IN ('scheduled','pending','verified') "
            "AND send_time IS NOT NULL AND send_time != '' AND send_time < ?",
            (cutoff.isoformat(),)
        ).fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE emails SET status='overdue' WHERE id IN ({placeholders})",
                ids
            )

    settings = _get_ar_settings()
    if not settings.get("enabled"):
        return

    window_minutes = max(5, int(settings.get("window_minutes", 60)))
    # Ensure the minimum reschedule delay clears the grace period, so the email
    # won't be immediately flagged overdue again on the next periodic run.
    min_delay_s = _OVERDUE_GRACE_SECONDS + 180   # grace + 3 min buffer
    max_delay_s = max(min_delay_s + 60, window_minutes * 60)

    with get_db() as conn:
        overdue = conn.execute(
            "SELECT id FROM emails WHERE status='overdue'"
        ).fetchall()
        # Transient AppleScript timeout failures are safe to retry automatically.
        timed_out = conn.execute(
            "SELECT id FROM emails WHERE status='failed' AND error LIKE '%timed out after%'"
        ).fetchall()
        to_reschedule = [r["id"] for r in overdue] + [r["id"] for r in timed_out]
        for eid in to_reschedule:
            delay = random.randint(min_delay_s, max_delay_s)
            new_time = now + timedelta(seconds=delay)
            conn.execute(
                "UPDATE emails SET send_time=?, send_time_str=?, status='scheduled', error=NULL WHERE id=?",
                (new_time.isoformat(), new_time.strftime("%Y-%m-%d %H:%M"), eid)
            )
            scheduler.add_job(_dispatch, trigger="date", run_date=new_time,
                              args=[eid], id=f"email_{eid}", replace_existing=True)
            _add_history(conn, eid, "rescheduled",
                         f"Auto-rescheduled to {new_time.strftime('%Y-%m-%d %H:%M')}")


# Run overdue detection + auto-reschedule every 2 minutes.
# First run is 90 s after startup so it doesn't interfere with _restore_scheduled_jobs.
scheduler.add_job(_auto_reschedule_job, trigger="interval", minutes=2,
                  id="overdue_checker", replace_existing=True,
                  next_run_time=datetime.now() + timedelta(seconds=90))


# ── Bounce detection ───────────────────────────────────────────────────────────

_BOUNCE_SUBJECTS = [
    "undeliverable", "delivery has failed", "delivery failure",
    "delivery status notification", "mail delivery failed",
    "returned mail", "nondelivery report", "non-delivery",
    "failure notice", "mailer-daemon",
]

_BOUNCE_SENDERS = ["mailer-daemon", "postmaster", "mail delivery subsystem", "mail delivery system"]

_BOUNCE_BODY_PHRASES = [
    "delivery has failed to these recipients",
    "the email address you entered couldn't be found",
    "address not found",
    "user unknown",
    "no such user",
    "mailbox not found",
    "does not exist",
    "recipient address rejected",
    "undeliverable address",
]


def _extract_bounced_address(subject, body, skip_addrs=None):
    """Try to pull the failed recipient email from a bounce message."""
    import re
    skip_domains = {
        "microsoft.com", "outlook.com", "office365.com", "exchange.microsoft.com",
        "googlemail.com", "google.com",
    }
    skip_users = {"mailer-daemon", "postmaster", "noreply", "no-reply"}
    skip_lower = {s.lower() for s in (skip_addrs or [])}

    addr_re = r'[\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,}'

    def _ok(addr):
        return (
            addr.split("@")[0].lower() not in skip_users
            and addr.split("@")[-1].lower() not in skip_domains
            and addr.lower() not in skip_lower
        )

    # Priority pass: look for addresses near delivery-failure language or in angle brackets
    priority_patterns = [
        r"(?:wasn't|was not|not|failed to be|could not be)\s+delivered\s+to\s+(" + addr_re + r")",
        r"delivery\s+(?:has\s+)?failed\s+(?:for\s+)?(?:to\s+)?(?:these\s+recipients[:\s]+)?(" + addr_re + r")",
        r"recipient[:\s]+(" + addr_re + r")",
        r"<(" + addr_re + r")>",
    ]
    for pattern in priority_patterns:
        m = re.search(pattern, body, re.IGNORECASE)
        if m and _ok(m.group(1)):
            return m.group(1)

    # Fallback: first non-system, non-sender address found anywhere in body
    for addr in re.findall(addr_re, body):
        if _ok(addr):
            return addr
    return None


def _check_bounces():
    """Poll Apple Mail inbox for bounce/NDR messages and mark matching emails as failed."""
    import re

    # Collect the user's own sending account addresses so we don't misidentify them
    # as the bounced recipient when they appear in bounce email headers.
    own_addrs = set()
    if selected_account_email:
        own_addrs.add(selected_account_email)
    try:
        with get_db() as conn:
            for row in conn.execute("SELECT DISTINCT account_email FROM batches WHERE account_email IS NOT NULL AND account_email != ''"):
                own_addrs.add(row[0])
    except Exception:
        pass

    # Pass 1 — get message ID, subject, sender of all unread messages across every account.
    # We write one line per message using a tab separator so no body content can corrupt parsing.
    list_script = """\
tell application "Mail"
    set cutoff to (current date) - (30 * days)
    set out to ""
    repeat with a in every account
        set inb to missing value
        repeat with mbname in {"INBOX", "Inbox"}
            try
                set inb to mailbox mbname of a
                exit repeat
            end try
        end repeat
        if inb is not missing value then
            try
                set msgs to (messages of inb whose date received >= cutoff)
                repeat with m in msgs
                    set out to out & (message id of m) & tab & (subject of m) & tab & (sender of m) & linefeed
                end repeat
            end try
        end if
    end repeat
    return out
end tell"""
    try:
        r = subprocess.run(["osascript", "-e", list_script],
                           capture_output=True, text=True, timeout=120)
    except Exception:
        return

    candidates = []
    for line in r.stdout.splitlines():
        parts = line.strip().split("\t", 2)
        if len(parts) != 3:
            continue
        msg_id, subject, sender = parts
        msg_id = msg_id.strip()
        if not msg_id:
            continue
        subj_l = subject.lower()
        sndr_l = sender.lower()
        if (any(p in subj_l for p in _BOUNCE_SUBJECTS)
                or any(p in sndr_l for p in _BOUNCE_SENDERS)):
            candidates.append({"id": msg_id, "subject": subject, "sender": sender})

    for cand in candidates:
        msg_id = cand["id"]

        with get_db() as conn:
            if conn.execute("SELECT 1 FROM processed_bounces WHERE message_id=?",
                            (msg_id,)).fetchone():
                continue

        # Pass 2 — fetch body of this specific bounce candidate.
        esc_id = msg_id.replace("\\", "\\\\").replace('"', '\\"')
        body_script = f"""\
tell application "Mail"
    repeat with a in every account
        set inb to missing value
        repeat with mbname in {{"INBOX", "Inbox"}}
            try
                set inb to mailbox mbname of a
                exit repeat
            end try
        end repeat
        if inb is not missing value then
            try
                set found to (messages of inb whose message id is "{esc_id}")
                if (count of found) > 0 then
                    return content of item 1 of found
                end if
            end try
        end if
    end repeat
    return ""
end tell"""
        try:
            r2 = subprocess.run(["osascript", "-e", body_script],
                                capture_output=True, text=True, timeout=30)
            body = r2.stdout.strip()
        except Exception:
            body = ""

        failed_addr = _extract_bounced_address(cand["subject"] + " " + body, body, skip_addrs=own_addrs)

        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_bounces (message_id, processed_at) VALUES (?,?)",
                (msg_id, datetime.now().isoformat())
            )
            if failed_addr:
                row = conn.execute(
                    "SELECT id FROM emails "
                    "WHERE LOWER(to_addr)=LOWER(?) AND status='sent' "
                    "ORDER BY sent_at DESC LIMIT 1",
                    (failed_addr,)
                ).fetchone()
                if row:
                    eid = row["id"]
                    conn.execute(
                        "UPDATE emails SET status='failed', error=? WHERE id=?",
                        (f"Delivery failed: bounce received for {failed_addr}", eid)
                    )
                    _add_history(conn, eid, "status",
                                 f"Marked failed: bounce/NDR received for {failed_addr}")
                    print(f"  [bounce] Marked email {eid} ({failed_addr}) as failed")

        # Mark the bounce as read so it doesn't re-trigger
        mark_script = f"""\
tell application "Mail"
    repeat with a in every account
        set inb to missing value
        repeat with mbname in {{"INBOX", "Inbox"}}
            try
                set inb to mailbox mbname of a
                exit repeat
            end try
        end repeat
        if inb is not missing value then
            try
                set found to (messages of inb whose message id is "{esc_id}")
                if (count of found) > 0 then
                    set read status of item 1 of found to true
                end if
            end try
        end if
    end repeat
end tell"""
        try:
            subprocess.run(["osascript", "-e", mark_script],
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass


def _fetch_mail_body(msg_id):
    """Fetch the plain-text content of one inbox message by its message id.
    Reuses the per-message body-fetch pattern from the bounce checker."""
    esc_id = msg_id.replace("\\", "\\\\").replace('"', '\\"')
    body_script = f"""\
tell application "Mail"
    repeat with a in every account
        set inb to missing value
        repeat with mbname in {{"INBOX", "Inbox"}}
            try
                set inb to mailbox mbname of a
                exit repeat
            end try
        end repeat
        if inb is not missing value then
            try
                set found to (messages of inb whose message id is "{esc_id}")
                if (count of found) > 0 then
                    return content of item 1 of found
                end if
            end try
        end if
    end repeat
    return ""
end tell"""
    try:
        r = subprocess.run(["osascript", "-e", body_script],
                           capture_output=True, text=True, timeout=30)
        return r.stdout
    except Exception:
        return ""


def _reply_snippet(body):
    """Trim a reply body to a stored snippet: drop the quoted original thread and
    cap the length so the DB stays small."""
    if not body:
        return ""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    for line in text.split("\n"):
        l = line.strip()
        # Stop once the quoted original message / thread begins.
        if l.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", l):
            break
        if l in ("-----Original Message-----", "________________________________"):
            break
        if l.startswith("From:") and kept:
            break
        kept.append(line)
    snippet = "\n".join(kept).strip() or text.strip()
    return snippet[:500].strip()


# Responses that arrive within this window are treated as automated replies
# (out-of-office / auto-acknowledgements) rather than genuine human replies.
_AUTO_REPLY_MAX_SECONDS = 360  # 6 minutes


def _check_replies(batch_id=None):
    """Scan the Apple Mail inbox for replies from people we've emailed and mark the
    matching sent email as 'replied', capturing the reply's body snippet, the time
    it arrived (reply_received_at), and how long it took (reply_delta_seconds).

    Targets two kinds of rows: emails still at response_status='none' (new replies),
    and emails already marked 'replied' but missing reply_received_at (backfill the
    timing for replies that were recorded before timing was captured / set by hand).
    Returns the number of emails updated."""
    params = []
    where = ("WHERE status='sent' AND to_addr IS NOT NULL AND to_addr != '' "
             "AND (response_status='none' "
             "OR (response_status='replied' AND reply_received_at IS NULL))")
    if batch_id:
        where += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        sent_rows = conn.execute(
            f"SELECT id, to_addr, sent_at FROM emails {where}", params).fetchall()
    if not sent_rows:
        return 0
    # Map each emailed address (lowercased) -> (email id, sent datetime).
    targets = {}
    for row in sent_rows:
        addr = (row["to_addr"] or "").strip().lower()
        if not addr:
            continue
        try:
            sent_dt = datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None
        except Exception:
            sent_dt = None
        targets[addr] = (row["id"], sent_dt)

    # Pass 1 — list sender, message id, and received date of every recent inbox
    # message. One line per message, tab-separated, so message content can't
    # corrupt parsing (an AppleScript list return would comma-join and break).
    list_script = """\
tell application "Mail"
    set cutoff to (current date) - (60 * days)
    set out to ""
    repeat with a in every account
        set inb to missing value
        repeat with mbname in {"INBOX", "Inbox"}
            try
                set inb to mailbox mbname of a
                exit repeat
            end try
        end repeat
        if inb is not missing value then
            try
                set msgs to (messages of inb whose date received >= cutoff)
                repeat with m in msgs
                    try
                        set d to date received of m
                        set out to out & (sender of m) & tab & (message id of m) & tab & (year of d) & tab & ((month of d) as integer) & tab & (day of d) & tab & (hours of d) & tab & (minutes of d) & tab & (seconds of d) & linefeed
                    end try
                end repeat
            end try
        end if
    end repeat
    return out
end tell"""
    try:
        r = subprocess.run(["osascript", "-e", list_script],
                           capture_output=True, text=True, timeout=120)
    except Exception:
        return 0

    # For each emailed address, collect (reply_dt, message_id) candidates.
    candidates = {}
    for line in r.stdout.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 8:
            continue
        sender, msg_id = parts[0], parts[1].strip()
        m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", sender)
        if not m:
            continue
        addr = m.group(0).lower()
        if addr not in targets:
            continue
        try:
            y, mo, dd, hh, mi, ss = (int(x) for x in parts[2:8])
            reply_dt = datetime(y, mo, dd, hh, mi, ss)
        except Exception:
            continue
        candidates.setdefault(addr, []).append((reply_dt, msg_id))

    marked = 0
    for addr, items in candidates.items():
        eid, sent_dt = targets[addr]
        items.sort(key=lambda t: t[0])
        # Only messages received at/after we sent can be a reply; fall back to all
        # of them if none qualify (clock skew between Mail and this machine).
        pool = [(dt, mid) for dt, mid in items if (sent_dt is None or dt >= sent_dt)] or items
        if not pool:
            continue
        # A response that lands within a few minutes is almost always an auto-reply
        # (out-of-office / auto-acknowledgement), not a human one. Prefer the
        # earliest genuinely-human reply; only if every candidate is that fast do we
        # record it as an automated response instead.
        chosen, status = None, "replied"
        for dt, mid in pool:
            secs = (dt - sent_dt).total_seconds() if sent_dt else None
            if secs is None or secs > _AUTO_REPLY_MAX_SECONDS:
                chosen = (dt, mid); status = "replied"; break
        if chosen is None:
            chosen = pool[0]; status = "automated"
        reply_dt, msg_id = chosen
        delta = int((reply_dt - sent_dt).total_seconds()) if sent_dt else None
        snippet = _reply_snippet(_fetch_mail_body(msg_id)) if msg_id else ""
        with get_db() as conn:
            # Fill timing for both new replies and already-recorded rows that never
            # got a received time; keep any reply body already stored.
            cur = conn.execute(
                "UPDATE emails SET response_status=?, "
                "reply_body=CASE WHEN reply_body IS NULL OR reply_body='' THEN ? ELSE reply_body END, "
                "reply_received_at=?, reply_delta_seconds=? "
                "WHERE id=? AND (response_status='none' OR reply_received_at IS NULL)",
                (status, snippet, reply_dt.isoformat(), delta, eid))
            if cur.rowcount:
                label = "Automated response" if status == "automated" else "Reply"
                _add_history(conn, eid, "response", f"{label} detected from {addr}")
                marked += 1
                print(f"  [reply] email {eid} ({addr}) {status} — received {reply_dt.isoformat()}")
    return marked


def _check_replies_job():
    """Background wrapper: respect the user's enabled flag and never raise."""
    try:
        if not _get_reply_settings().get("enabled", True):
            return
        _check_replies()
    except Exception as exc:
        print(f"  [reply] periodic check failed: {exc}")


scheduler.add_job(_check_bounces, trigger="interval", minutes=5, id="bounce_checker",
                  replace_existing=True, next_run_time=datetime.now())

# Periodic reply scan — interval/on-off come from saved settings (hourly default).
_apply_reply_schedule()


# ── Business hours scheduler ───────────────────────────────────────────────────

def _business_timestamps(start_dt: datetime, end_dt: datetime, count: int,
                         biz_start_h: int = 8, biz_end_h: int = 17,
                         biz_days: list = None) -> list:
    if biz_days is None:
        biz_days = [0, 1, 2, 3, 4]  # Mon–Fri
    slots = []
    d = start_dt.date()
    while d <= end_dt.date():
        if d.weekday() in biz_days:
            day_start = datetime.combine(d, time(biz_start_h, 0))
            day_end = datetime.combine(d, time(biz_end_h, 0))
            actual_start = max(day_start, start_dt)
            actual_end = min(day_end, end_dt)
            if actual_start < actual_end:
                slots.append((actual_start, actual_end))
        d += timedelta(days=1)
    if not slots:
        raise ValueError("No business hours in the selected range. Check your selected days and date window.")
    result = []
    for _ in range(count):
        ds, de = random.choice(slots)
        secs = random.uniform(0, (de - ds).total_seconds())
        result.append(ds + timedelta(seconds=secs))
    return sorted(result)


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _clean(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val)


def _make_record(row, cols, now, batch_id, professor_mode=False) -> dict:
    if "send_time" in cols and _clean(row.get("send_time")):
        try:
            dt = pd.to_datetime(row["send_time"])
            iso = dt.isoformat()
            display = dt.strftime("%Y-%m-%d %H:%M")
            status = "pending" if dt > now else "overdue"
        except Exception:
            iso = None; display = _clean(row.get("send_time", "")); status = "invalid"
    else:
        iso = None; display = ""; status = "pending"

    if status != "invalid":
        status = "needs_review"

    return {
        "batch_id": batch_id,
        "to_addr": _clean(row.get("to", "")),
        "cc": _clean(row.get("cc", "")) if "cc" in cols else "",
        "bcc": _clean(row.get("bcc", "")) if "bcc" in cols else "",
        "subject": _clean(row.get("subject", "")),
        "body": _clean(row.get("body", "")).replace("\\n", "\n"),
        "attachments": _clean(row.get("attachments", "")) if "attachments" in cols else "",
        "send_time": iso,
        "send_time_str": display,
        "status": status,
        "created_at": now.isoformat(),
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Batches ────────────────────────────────────────────────────────────────────

@app.route("/api/batches")
def list_batches():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT b.*,
                COUNT(e.id)                                                         AS total,
                SUM(CASE WHEN e.status='sent'         THEN 1 ELSE 0 END)           AS sent,
                SUM(CASE WHEN e.status='pending'      THEN 1 ELSE 0 END)           AS pending,
                SUM(CASE WHEN e.status='scheduled'    THEN 1 ELSE 0 END)           AS scheduled,
                SUM(CASE WHEN e.status='failed'       THEN 1 ELSE 0 END)           AS failed,
                SUM(CASE WHEN e.status='overdue'      THEN 1 ELSE 0 END)           AS overdue,
                SUM(CASE WHEN e.status='needs_review' THEN 1 ELSE 0 END)           AS needs_review,
                SUM(CASE WHEN e.status='verified'     THEN 1 ELSE 0 END)           AS verified,
                SUM(CASE WHEN e.response_status='replied' THEN 1 ELSE 0 END)       AS replied,
                SUM(CASE WHEN e.duplicate_warning=1 OR e.content_warning=1 THEN 1 ELSE 0 END) AS warnings
            FROM batches b
            LEFT JOIN emails e ON e.batch_id = b.id
            GROUP BY b.id ORDER BY b.created_at DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/batches", methods=["POST"])
def create_batch():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO batches (name, notes, professor_mode, account_email, created_at) VALUES (?,?,?,?,?)",
            (name, data.get("notes", ""), 1 if data.get("professor_mode") else 0,
             data.get("account_email", ""), datetime.now().isoformat())
        )
        row = conn.execute("SELECT * FROM batches WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/batches/<int:bid>", methods=["GET"])
def get_batch(bid):
    with get_db() as conn:
        row = conn.execute("""
            SELECT b.*,
                COUNT(e.id)                                                         AS total,
                SUM(CASE WHEN e.status='sent'         THEN 1 ELSE 0 END)           AS sent,
                SUM(CASE WHEN e.status='pending'      THEN 1 ELSE 0 END)           AS pending,
                SUM(CASE WHEN e.status='scheduled'    THEN 1 ELSE 0 END)           AS scheduled,
                SUM(CASE WHEN e.status='failed'       THEN 1 ELSE 0 END)           AS failed,
                SUM(CASE WHEN e.status='needs_review' THEN 1 ELSE 0 END)           AS needs_review,
                SUM(CASE WHEN e.status='verified'     THEN 1 ELSE 0 END)           AS verified,
                SUM(CASE WHEN e.response_status='replied' THEN 1 ELSE 0 END)       AS replied
            FROM batches b LEFT JOIN emails e ON e.batch_id=b.id
            WHERE b.id=? GROUP BY b.id
        """, (bid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@app.route("/api/batches/<int:bid>", methods=["PATCH"])
def update_batch(bid):
    data = request.json or {}
    with get_db() as conn:
        if not conn.execute("SELECT id FROM batches WHERE id=?", (bid,)).fetchone():
            return jsonify({"error": "Not found"}), 404
        if "name" in data and data["name"].strip():
            conn.execute("UPDATE batches SET name=? WHERE id=?", (data["name"].strip(), bid))
        if "notes" in data:
            conn.execute("UPDATE batches SET notes=? WHERE id=?", (data["notes"], bid))
        if "professor_mode" in data:
            conn.execute("UPDATE batches SET professor_mode=? WHERE id=?",
                         (1 if data["professor_mode"] else 0, bid))
        if "account_email" in data:
            conn.execute("UPDATE batches SET account_email=? WHERE id=?", (data["account_email"], bid))
        row = conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/batches/<int:bid>", methods=["DELETE"])
def delete_batch(bid):
    with get_db() as conn:
        if not conn.execute("SELECT id FROM batches WHERE id=?", (bid,)).fetchone():
            return jsonify({"error": "Not found"}), 404
        eids = [r["id"] for r in conn.execute("SELECT id FROM emails WHERE batch_id=?", (bid,)).fetchall()]
    for eid in eids:
        try: scheduler.remove_job(f"email_{eid}")
        except Exception: pass
    with get_db() as conn:
        conn.execute("DELETE FROM batches WHERE id=?", (bid,))
    return jsonify({"success": True})


@app.route("/api/batches/<int:bid>/review-queue")
def batch_review_queue(bid):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM emails WHERE batch_id=? AND status='needs_review' ORDER BY id ASC", (bid,)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM emails WHERE batch_id=?", (bid,)).fetchone()[0]
    return jsonify({"emails": [row_to_dict(r) for r in rows], "total": total, "needs_review": len(rows)})


# ── Profiles ───────────────────────────────────────────────────────────────────

@app.route("/api/profiles")
def list_profiles():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/profiles", methods=["POST"])
def create_profile():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO profiles (name, account_email, attachments, signature, cc_default, bcc_default, "
            "professor_mode, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, data.get("account_email", ""), data.get("attachments", ""),
             data.get("signature", ""), data.get("cc_default", ""), data.get("bcc_default", ""),
             1 if data.get("professor_mode") else 0, data.get("notes", ""), datetime.now().isoformat())
        )
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/profiles/<int:pid>", methods=["GET"])
def get_profile(pid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@app.route("/api/profiles/<int:pid>", methods=["PATCH"])
def update_profile(pid):
    data = request.json or {}
    with get_db() as conn:
        if not conn.execute("SELECT id FROM profiles WHERE id=?", (pid,)).fetchone():
            return jsonify({"error": "Not found"}), 404
        for f in ["name", "account_email", "attachments", "signature", "cc_default", "bcc_default", "notes"]:
            if f in data:
                conn.execute(f"UPDATE profiles SET {f}=? WHERE id=?", (data[f], pid))
        if "professor_mode" in data:
            conn.execute("UPDATE profiles SET professor_mode=? WHERE id=?",
                         (1 if data["professor_mode"] else 0, pid))
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/profiles/<int:pid>", methods=["DELETE"])
def delete_profile(pid):
    with get_db() as conn:
        if not conn.execute("SELECT id FROM profiles WHERE id=?", (pid,)).fetchone():
            return jsonify({"error": "Not found"}), 404
        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
    return jsonify({"success": True})


@app.route("/api/profiles/<int:pid>/apply-to-batch", methods=["POST"])
def apply_profile_to_batch(pid):
    data = request.json or {}
    with get_db() as conn:
        prof = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
        if not prof: return jsonify({"error": "Not found"}), 404
        batch_id = data.get("batch_id")
        extra = "WHERE status NOT IN ('sent','sending')"
        params = []
        if batch_id:
            extra += " AND batch_id=?"; params.append(int(batch_id))
        rows = conn.execute(f"SELECT id FROM emails {extra}", params).fetchall()
        count = 0
        for row in rows:
            eid = row["id"]; updates, vals = [], []
            if prof["cc_default"]: updates.append("cc=?"); vals.append(prof["cc_default"])
            if prof["bcc_default"]: updates.append("bcc=?"); vals.append(prof["bcc_default"])
            if prof["attachments"]: updates.append("attachments=?"); vals.append(prof["attachments"])
            if updates:
                updates.append("manually_edited=1"); vals.append(eid)
                conn.execute(f"UPDATE emails SET {', '.join(updates)} WHERE id=?", vals)
                _add_history(conn, eid, "edited", f"Profile '{prof['name']}' applied")
                count += 1
    return jsonify({"success": True, "count": count, "profile": prof["name"]})


# ── Emails ─────────────────────────────────────────────────────────────────────

@app.route("/api/emails")
def get_emails():
    page = max(1, int(request.args.get("page", 1)))
    per_page = max(1, int(request.args.get("per_page", 50)))
    status_filter = request.args.get("status", "")
    batch_id = request.args.get("batch_id", "")
    duplicate_warning = request.args.get("duplicate_warning", "")
    has_warning = request.args.get("has_warning", "")

    clauses, params = [], []
    if status_filter:
        clauses.append("status=?"); params.append(status_filter)
    if batch_id:
        clauses.append("batch_id=?"); params.append(int(batch_id))
    if duplicate_warning:
        clauses.append("duplicate_warning=?"); params.append(int(duplicate_warning))
    if has_warning:
        clauses.append("(duplicate_warning=1 OR content_warning=1)")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM emails {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM emails {where} ORDER BY send_time ASC NULLS LAST, id ASC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page]
        ).fetchall()
    return jsonify({
        "emails": [row_to_dict(r) for r in rows],
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })


@app.route("/api/emails", methods=["POST"])
def create_email():
    data = request.json or {}
    now = datetime.now()
    to_addr = (data.get("to") or "").strip()
    if not to_addr:
        return jsonify({"error": "To address required"}), 400
    send_time, send_time_str, status = None, "", "pending"
    if data.get("send_time"):
        try:
            dt = datetime.fromisoformat(data["send_time"])
            send_time = dt.isoformat(); send_time_str = dt.strftime("%Y-%m-%d %H:%M")
            status = "pending" if dt > now else "overdue"
        except Exception: pass
    with get_db() as conn:
        dup = _check_duplicate(conn, to_addr)
        body = data.get("body", ""); subject = data.get("subject", "")
        cur = conn.execute(
            "INSERT INTO emails (batch_id, to_addr, cc, bcc, subject, body, attachments,"
            "send_time, send_time_str, status, duplicate_warning, content_warning, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data.get("batch_id"), to_addr, data.get("cc", ""), data.get("bcc", ""),
             subject, body, data.get("attachments", ""),
             send_time, send_time_str, status,
             1 if dup else 0, 1 if _has_content_warning(body, subject) else 0,
             data.get("notes", ""), now.isoformat())
        )
        eid = cur.lastrowid
        _add_history(conn, eid, "created", "Email created")
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.route("/api/emails/<int:eid>")
def get_email(eid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not row: return jsonify({"error": "Not found"}), 404
        hist = conn.execute(
            "SELECT * FROM email_history WHERE email_id=? ORDER BY changed_at DESC LIMIT 100", (eid,)
        ).fetchall()
    d = row_to_dict(row); d["history"] = [dict(h) for h in hist]
    return jsonify(d)


@app.route("/api/emails/<int:eid>", methods=["PATCH"])
def update_email(eid):
    data = request.json or {}
    now = datetime.now()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not row: return jsonify({"error": "Not found"}), 404
        old = row_to_dict(row); changed_old, changed_new = {}, {}
        for field, col in [("to", "to_addr"), ("cc", "cc"), ("bcc", "bcc"),
                            ("subject", "subject"), ("body", "body"),
                            ("attachments", "attachments"), ("notes", "notes")]:
            if field in data:
                val = str(data[field])
                conn.execute(f"UPDATE emails SET {col}=? WHERE id=?", (val, eid))
                changed_old[field] = old.get(field, ""); changed_new[field] = val
        if "batch_id" in data:
            conn.execute("UPDATE emails SET batch_id=? WHERE id=?", (data["batch_id"], eid))
        if "dismiss_warning" in data and data["dismiss_warning"]:
            conn.execute("UPDATE emails SET duplicate_warning=2 WHERE id=?", (eid,))
        if "send_time" in data and data["send_time"]:
            try:
                dt = datetime.fromisoformat(data["send_time"])
                ns = "pending" if dt > now else "overdue"
                conn.execute("UPDATE emails SET send_time=?, send_time_str=?, status=? WHERE id=?",
                             (dt.isoformat(), dt.strftime("%Y-%m-%d %H:%M"), ns, eid))
                changed_old["send_time"] = old.get("send_time"); changed_new["send_time"] = dt.isoformat()
            except Exception: pass
        conn.execute("UPDATE emails SET manually_edited=1 WHERE id=?", (eid,))
        if "body" in data or "subject" in data:
            fresh = conn.execute("SELECT body, subject FROM emails WHERE id=?", (eid,)).fetchone()
            cw = 1 if _has_content_warning(fresh["body"], fresh["subject"]) else 0
            conn.execute("UPDATE emails SET content_warning=? WHERE id=?", (cw, eid))
        if changed_old:
            _add_history(conn, eid, "edited", "Edited: " + ", ".join(changed_old.keys()), changed_old, changed_new)
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/api/emails/<int:eid>", methods=["DELETE"])
def delete_email(eid):
    with get_db() as conn:
        if not conn.execute("SELECT id FROM emails WHERE id=?", (eid,)).fetchone():
            return jsonify({"error": "Not found"}), 404
    try: scheduler.remove_job(f"email_{eid}")
    except Exception: pass
    with get_db() as conn:
        conn.execute("DELETE FROM emails WHERE id=?", (eid,))
    return jsonify({"success": True})


@app.route("/api/emails/<int:eid>/status", methods=["PATCH"])
def set_email_status(eid):
    data = request.json or {}
    new_status = data.get("status", "")
    allowed = {"pending", "overdue", "skipped", "failed", "needs_review", "verified"}
    if new_status not in allowed:
        return jsonify({"error": f"Must be one of: {', '.join(sorted(allowed))}"}), 400
    with get_db() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not row: return jsonify({"error": "Not found"}), 404
        old_status = row["status"]
        conn.execute("UPDATE emails SET status=?, manually_edited=1 WHERE id=?", (new_status, eid))
        _add_history(conn, eid, "status", f"Status: {old_status} → {new_status}",
                     {"status": old_status}, {"status": new_status})
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/api/emails/<int:eid>/verify", methods=["POST"])
def verify_email(eid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not row: return jsonify({"error": "Not found"}), 404
        conn.execute("UPDATE emails SET status='verified', manually_edited=1 WHERE id=?", (eid,))
        _add_history(conn, eid, "status", "Email verified")
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/api/emails/<int:eid>/unverify", methods=["POST"])
def unverify_email(eid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not row: return jsonify({"error": "Not found"}), 404
        conn.execute("UPDATE emails SET status='needs_review', manually_edited=1 WHERE id=?", (eid,))
        _add_history(conn, eid, "status", "Returned to review queue")
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/api/emails/<int:eid>/response", methods=["PATCH"])
def set_response(eid):
    data = request.json or {}
    rs = data.get("response_status", "none")
    allowed = {"none", "replied", "automated", "bounced"}
    if rs not in allowed: return jsonify({"error": "Invalid response status"}), 400
    with get_db() as conn:
        if not conn.execute("SELECT id FROM emails WHERE id=?", (eid,)).fetchone():
            return jsonify({"error": "Not found"}), 404
        conn.execute("UPDATE emails SET response_status=? WHERE id=?", (rs, eid))
        _add_history(conn, eid, "response", f"Response marked: {rs}")
        row = conn.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/api/emails/<int:eid>/send-now", methods=["POST"])
def send_now(eid):
    with get_db() as conn:
        if not conn.execute("SELECT id FROM emails WHERE id=?", (eid,)).fetchone():
            return jsonify({"error": "Not found"}), 404
    threading.Thread(target=_dispatch, args=(eid,), daemon=True).start()
    return jsonify({"success": True})


# ── Stats ──────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def get_stats():
    batch_id = request.args.get("batch_id", "")
    params = []; extra = "WHERE 1=1"
    if batch_id:
        extra += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        status_rows = conn.execute(
            f"SELECT status, COUNT(*) AS cnt FROM emails {extra} GROUP BY status", params
        ).fetchall()
        agg = conn.execute(
            f"SELECT COUNT(*) AS total,"
            f" SUM(CASE WHEN response_status='replied' THEN 1 ELSE 0 END) AS replied,"
            f" SUM(CASE WHEN duplicate_warning=1 OR content_warning=1 THEN 1 ELSE 0 END) AS warnings"
            f" FROM emails {extra}", params
        ).fetchone()
    counts = {
        "total": agg["total"] or 0, "pending": 0, "overdue": 0, "scheduled": 0,
        "sending": 0, "sent": 0, "failed": 0, "skipped": 0, "invalid": 0,
        "replied": agg["replied"] or 0, "needs_review": 0, "verified": 0,
        "warnings": agg["warnings"] or 0,
    }
    for r in status_rows:
        if r["status"] in counts:
            counts[r["status"]] = r["cnt"]
    return jsonify(counts)


def _classify_error(err):
    """Bucket a failure error message into a delivery-problem category."""
    e = (err or "").lower()
    if "timed out" in e or "timeout" in e:
        return "timed_out"
    if any(k in e for k in ("bounce", "delivery failed", "undeliverable",
                            "does not exist", "no such", "recipient", "mailbox", "not found")):
        return "bounced"
    if any(k in e for k in ("auth", "535", "password", "login", "credential")):
        return "auth"
    if any(k in e for k in ("smtp", "connection", "network", "refused", "connect")):
        return "network"
    if any(k in e for k in ("mail got an error", "applescript", "osascript",
                            "execution error", "-1728", "-1700")):
        return "mailapp"
    return "other"


@app.route("/api/insights")
def get_insights():
    """Full breakdown of what happened to emails — by status, failure type, and
    response outcome — scoped to a batch (?batch_id=) or across all emails."""
    batch_id = request.args.get("batch_id", "")
    params = []; where = "WHERE 1=1"
    if batch_id:
        where += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        status_rows = conn.execute(
            f"SELECT status, COUNT(*) AS c FROM emails {where} GROUP BY status", params).fetchall()
        resp_rows = conn.execute(
            f"SELECT response_status AS rs, COUNT(*) AS c FROM emails {where} GROUP BY response_status", params).fetchall()
        fail_rows = conn.execute(
            f"SELECT error FROM emails {where} AND status='failed'", params).fetchall()
        total = conn.execute(f"SELECT COUNT(*) AS c FROM emails {where}", params).fetchone()["c"]
        timing = conn.execute(
            f"SELECT COUNT(*) AS c, AVG(reply_delta_seconds) AS avg,"
            f" MIN(reply_delta_seconds) AS fastest, MAX(reply_delta_seconds) AS slowest"
            f" FROM emails {where} AND reply_delta_seconds IS NOT NULL", params).fetchone()
        tl_rows = conn.execute(
            f"SELECT substr(reply_received_at,1,10) AS day, COUNT(*) AS c"
            f" FROM emails {where} AND reply_received_at IS NOT NULL AND reply_received_at != ''"
            f" GROUP BY day ORDER BY day", params).fetchall()
    status = {s: 0 for s in ("needs_review", "verified", "pending", "scheduled",
                             "overdue", "sending", "sent", "skipped", "invalid", "failed")}
    for r in status_rows:
        if r["status"] in status:
            status[r["status"]] = r["c"]
    responses = {s: 0 for s in ("none", "replied", "automated", "bounced")}
    for r in resp_rows:
        if r["rs"] in responses:
            responses[r["rs"]] = r["c"]
    failures = {s: 0 for s in ("bounced", "timed_out", "auth", "network", "mailapp", "other")}
    for r in fail_rows:
        failures[_classify_error(r["error"])] += 1
    sent = status["sent"]
    replies = responses["replied"]
    reply_metrics = {
        "sent": sent,
        "replies": replies,
        "reply_rate": round(replies / sent, 4) if sent else 0,
        "delivery_rate": round((sent - failures["bounced"]) / sent, 4) if sent else 0,
        "with_timing": timing["c"] or 0,
        "avg_seconds": int(timing["avg"]) if timing["avg"] is not None else None,
        "fastest_seconds": timing["fastest"],
        "slowest_seconds": timing["slowest"],
    }
    timeline = [{"day": r["day"], "count": r["c"]} for r in tl_rows]
    return jsonify({"total": total, "status": status,
                    "responses": responses, "failures": failures,
                    "reply_metrics": reply_metrics, "timeline": timeline})


@app.route("/api/replies")
def get_replies():
    """List the emails that got a reply, scoped to a batch (?batch_id=) or across
    all emails."""
    batch_id = request.args.get("batch_id", "")
    where = "WHERE e.response_status = 'replied'"
    params = []
    if batch_id:
        where += " AND e.batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT e.id, e.to_addr, e.subject, e.response_status, e.sent_at,"
            f" e.reply_body, e.reply_received_at, e.reply_delta_seconds,"
            f" b.name AS batch_name"
            f" FROM emails e LEFT JOIN batches b ON b.id=e.batch_id {where}"
            f" ORDER BY e.reply_received_at DESC, e.sent_at DESC, e.id DESC", params
        ).fetchall()
    return jsonify({"replies": [dict(r) for r in rows]})


# ── Warnings ───────────────────────────────────────────────────────────────────

@app.route("/api/warnings")
def get_warnings():
    batch_id = request.args.get("batch_id", "")
    with get_db() as conn:
        dw = "WHERE e.duplicate_warning IN (1,2)"
        dp = []
        if batch_id:
            dw += " AND e.batch_id=?"; dp.append(int(batch_id))
        dupes = conn.execute(
            f"SELECT e.id, e.to_addr, e.batch_id, e.status, e.duplicate_warning,"
            f" b.name AS batch_name, s.first_sent_at, s.send_count"
            f" FROM emails e LEFT JOIN batches b ON b.id=e.batch_id"
            f" LEFT JOIN sent_address_log s ON LOWER(s.email_addr)=LOWER(e.to_addr)"
            f" {dw} ORDER BY e.created_at DESC", dp
        ).fetchall()
        # Every failed email counts — including ones with no stored error text —
        # so this matches the Failed total shown elsewhere.
        fw = "WHERE e.status='failed'"
        fp = []
        if batch_id:
            fw += " AND e.batch_id=?"; fp.append(int(batch_id))
        failures = conn.execute(
            f"SELECT e.id, e.to_addr, e.subject, e.error, e.sent_at, b.name AS batch_name"
            f" FROM emails e LEFT JOIN batches b ON b.id=e.batch_id"
            f" {fw} ORDER BY e.created_at DESC LIMIT 500", fp
        ).fetchall()
        cw = "WHERE e.content_warning=1"
        cp = []
        if batch_id:
            cw += " AND e.batch_id=?"; cp.append(int(batch_id))
        content_issues = conn.execute(
            f"SELECT e.id, e.to_addr, e.subject, e.status, b.name AS batch_name"
            f" FROM emails e LEFT JOIN batches b ON b.id=e.batch_id"
            f" {cw} ORDER BY e.created_at DESC LIMIT 100", cp
        ).fetchall()
    return jsonify({
        "duplicates": [dict(r) for r in dupes],
        "failures": [dict(r) for r in failures],
        "content_issues": [dict(r) for r in content_issues],
    })


# ── Bulk ops ───────────────────────────────────────────────────────────────────

@app.route("/api/emails/ids", methods=["GET"])
def get_email_ids():
    status_filter = request.args.get("status", "")
    batch_id = request.args.get("batch_id", "")
    duplicate_warning = request.args.get("duplicate_warning", "")
    has_warning = request.args.get("has_warning", "")
    clauses = ["1=1"]
    params = []
    if status_filter:
        clauses.append("status=?"); params.append(status_filter)
    if batch_id:
        clauses.append("batch_id=?"); params.append(int(batch_id))
    if duplicate_warning:
        clauses.append("duplicate_warning=1")
    if has_warning:
        clauses.append("(duplicate_warning=1 OR content_warning=1)")
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id FROM emails WHERE {' AND '.join(clauses)} ORDER BY id ASC", params
        ).fetchall()
    return jsonify({"ids": [r["id"] for r in rows], "total": len(rows)})


@app.route("/api/bulk-change-status", methods=["POST"])
def bulk_change_status():
    data = request.json or {}
    ids = data.get("ids", [])
    new_status = data.get("status", "")
    allowed = {"pending", "overdue", "skipped", "failed", "needs_review", "verified"}
    if new_status not in allowed:
        return jsonify({"error": f"Status must be one of: {', '.join(sorted(allowed))}"}), 400
    if not ids:
        return jsonify({"error": "No ids"}), 400
    with get_db() as conn:
        count = sum(
            conn.execute(
                "UPDATE emails SET status=?, manually_edited=1 WHERE id=? AND status NOT IN ('sent','sending')",
                (new_status, i)
            ).rowcount for i in ids
        )
    return jsonify({"success": True, "count": count})


@app.route("/api/bulk-verify", methods=["POST"])
def bulk_verify():
    data = request.json or {}
    ids = data.get("ids", [])
    with get_db() as conn:
        count = sum(
            conn.execute("UPDATE emails SET status='verified' WHERE id=? AND status='needs_review'", (i,)).rowcount
            for i in ids
        )
    return jsonify({"success": True, "count": count})


@app.route("/api/bulk-attach", methods=["POST"])
def bulk_attach():
    if "file" not in request.files: return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    filepath = os.path.join(UPLOADS_DIR, file.filename or "attachment")
    file.save(filepath)
    batch_id = request.form.get("batch_id")
    extra = "WHERE status NOT IN ('sent','sending','skipped')"; params = [filepath]
    if batch_id:
        extra += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        cur = conn.execute(f"UPDATE emails SET attachments=? {extra}", params)
    return jsonify({"success": True, "count": cur.rowcount, "filename": file.filename})


@app.route("/api/bulk-clear-attachments", methods=["POST"])
def bulk_clear_attachments():
    data = request.json or {}; batch_id = data.get("batch_id")
    extra = "WHERE status NOT IN ('sent','sending') AND attachments != ''"; params = []
    if batch_id:
        extra += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        cur = conn.execute(f"UPDATE emails SET attachments='' {extra}", params)
    return jsonify({"success": True, "count": cur.rowcount})


@app.route("/api/bulk-shift-time", methods=["POST"])
def bulk_shift_time():
    data = request.json or {}; delta_minutes = int(data.get("delta_minutes", 0))
    if delta_minutes == 0: return jsonify({"error": "Delta cannot be zero"}), 400
    batch_id = data.get("batch_id"); delta = timedelta(minutes=delta_minutes); now = datetime.now()
    extra = "WHERE status IN ('pending','overdue','scheduled','verified') AND send_time IS NOT NULL"; params = []
    if batch_id:
        extra += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        rows = conn.execute(f"SELECT id, send_time FROM emails {extra}", params).fetchall()
        count = 0
        for row in rows:
            try:
                dt = datetime.fromisoformat(row["send_time"]) + delta
                ns = "pending" if dt > now else "overdue"
                conn.execute("UPDATE emails SET send_time=?, send_time_str=?, status=? WHERE id=?",
                             (dt.isoformat(), dt.strftime("%Y-%m-%d %H:%M"), ns, row["id"]))
                count += 1
            except Exception: pass
    return jsonify({"success": True, "count": count})


@app.route("/api/bulk-skip", methods=["POST"])
def bulk_skip():
    data = request.json or {}; ids = data.get("ids", [])
    # When resolve=True, also clear any duplicate warning (mark it resolved) so a
    # skipped duplicate stops showing as an open warning.
    resolve = bool(data.get("resolve"))
    sql = ("UPDATE emails SET status='skipped'"
           + (", duplicate_warning=2" if resolve else "")
           + " WHERE id=? AND status NOT IN ('sent','sending')")
    with get_db() as conn:
        count = sum(conn.execute(sql, (i,)).rowcount for i in ids)
    return jsonify({"success": True, "count": count})


@app.route("/api/bulk-unskip", methods=["POST"])
def bulk_unskip():
    data = request.json or {}; ids = data.get("ids", []); now = datetime.now()
    with get_db() as conn:
        count = 0
        for i in ids:
            row = conn.execute("SELECT send_time, status FROM emails WHERE id=?", (i,)).fetchone()
            if not row or row["status"] != "skipped": continue
            try:
                dt = datetime.fromisoformat(row["send_time"]) if row["send_time"] else None
                ns = ("pending" if dt > now else "overdue") if dt else "pending"
            except Exception: ns = "pending"
            conn.execute("UPDATE emails SET status=? WHERE id=?", (ns, i)); count += 1
    return jsonify({"success": True, "count": count})


@app.route("/api/bulk-delete", methods=["POST"])
def bulk_delete():
    data = request.json or {}; ids = data.get("ids", [])
    for i in ids:
        try: scheduler.remove_job(f"email_{i}")
        except Exception: pass
    with get_db() as conn:
        count = sum(conn.execute("DELETE FROM emails WHERE id=?", (i,)).rowcount for i in ids)
    return jsonify({"success": True, "count": count})


@app.route("/api/bulk-set-time", methods=["POST"])
def bulk_set_time():
    data = request.json or {}
    ids = data.get("ids", [])
    send_time = data.get("send_time", "")
    if not ids: return jsonify({"error": "No ids"}), 400
    now = datetime.now()
    with get_db() as conn:
        count = 0
        for i in ids:
            row = conn.execute("SELECT status FROM emails WHERE id=?", (i,)).fetchone()
            if not row or row["status"] in ("sent", "sending"): continue
            if send_time:
                try:
                    dt = datetime.fromisoformat(send_time)
                    ns = "pending" if dt > now else "overdue"
                    conn.execute("UPDATE emails SET send_time=?, send_time_str=?, status=? WHERE id=?",
                                 (dt.isoformat(), dt.strftime("%Y-%m-%d %H:%M"), ns, i))
                except Exception: continue
            else:
                conn.execute("UPDATE emails SET send_time=NULL, send_time_str=NULL, status='pending' WHERE id=?", (i,))
            count += 1
    return jsonify({"success": True, "count": count})


@app.route("/api/bulk-move-batch", methods=["POST"])
def bulk_move_batch():
    data = request.json or {}
    ids = data.get("ids", [])
    batch_id = data.get("batch_id")
    if not ids: return jsonify({"error": "No ids"}), 400
    with get_db() as conn:
        count = sum(
            conn.execute("UPDATE emails SET batch_id=? WHERE id=? AND status NOT IN ('sent','sending')",
                         (batch_id, i)).rowcount
            for i in ids
        )
    return jsonify({"success": True, "count": count})


@app.route("/api/check-bounces", methods=["POST"])
def check_bounces_now():
    threading.Thread(target=_check_bounces, daemon=True).start()
    return jsonify({"ok": True, "message": "Bounce check started"})


@app.route("/api/check-replies", methods=["POST"])
def check_replies_now():
    """Scan the inbox for replies. Runs synchronously so the UI can report how
    many were found. Optional {batch_id} limits the scan to one batch."""
    data = request.json or {}
    batch_id = data.get("batch_id")
    try:
        marked = _check_replies(int(batch_id) if batch_id else None)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "marked": marked})


@app.route("/api/reset", methods=["POST"])
def reset():
    data = request.json or {}; batch_id = data.get("batch_id")
    if batch_id: return delete_batch(int(batch_id))
    for job in scheduler.get_jobs():
        if job.id.startswith("email_"):
            try: scheduler.remove_job(job.id)
            except Exception: pass
    with get_db() as conn:
        conn.execute("DELETE FROM email_history")
        conn.execute("DELETE FROM emails")
        conn.execute("DELETE FROM batches")
        conn.execute("DELETE FROM profiles")
    return jsonify({"success": True})


# ── Schedule ───────────────────────────────────────────────────────────────────

@app.route("/api/schedule", methods=["POST"])
def schedule_all():
    data = request.json or {}; batch_id = data.get("batch_id"); now = datetime.now()
    extra = "WHERE status IN ('pending','overdue','verified')"; params = []
    if batch_id:
        extra += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        rows = conn.execute(f"SELECT id, send_time FROM emails {extra}", params).fetchall()
    queued = sent_now = skipped = 0
    for row in rows:
        eid, iso = row["id"], row["send_time"]
        if not iso: skipped += 1; continue
        send_time = datetime.fromisoformat(iso)
        if send_time <= now:
            threading.Thread(target=_dispatch, args=(eid,), daemon=True).start(); sent_now += 1
        else:
            scheduler.add_job(_dispatch, trigger="date", run_date=send_time,
                              args=[eid], id=f"email_{eid}", replace_existing=True)
            with get_db() as conn:
                conn.execute("UPDATE emails SET status='scheduled' WHERE id=? AND status NOT IN ('sent','sending')", (eid,))
            queued += 1
    return jsonify({"queued": queued, "sent_now": sent_now, "skipped": skipped})


@app.route("/api/smart-schedule", methods=["POST"])
def smart_schedule():
    data = request.json or {}
    business_hours = bool(data.get("business_hours", False))
    biz_start_h = max(0, min(23, int(data.get("biz_start_h", 8))))
    biz_end_h = max(1, min(24, int(data.get("biz_end_h", 17))))
    biz_days = data.get("biz_days", [0, 1, 2, 3, 4])
    try:
        start = datetime.fromisoformat(data["start"])
        end = datetime.fromisoformat(data["end"])
    except (KeyError, ValueError):
        return jsonify({"error": "Invalid start or end time"}), 400
    if end <= start: return jsonify({"error": "End must be after start"}), 400
    batch_id = data.get("batch_id"); min_gap = max(0, int(data.get("min_gap", 0)))
    extra = "WHERE status IN ('pending','overdue','verified')"; params = []
    if batch_id:
        extra += " AND batch_id=?"; params.append(int(batch_id))
    with get_db() as conn:
        eids = [r["id"] for r in conn.execute(f"SELECT id FROM emails {extra}", params).fetchall()]
    if not eids: return jsonify({"error": "No pending or verified emails to distribute"}), 400
    try:
        if business_hours:
            timestamps = _business_timestamps(start, end, len(eids), biz_start_h, biz_end_h, biz_days)
        else:
            total_secs = (end - start).total_seconds()
            timestamps = sorted(start + timedelta(seconds=random.uniform(0, total_secs)) for _ in range(len(eids)))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if min_gap > 0:
        for k in range(1, len(timestamps)):
            if (timestamps[k] - timestamps[k-1]).total_seconds() < min_gap:
                timestamps[k] = timestamps[k-1] + timedelta(seconds=min_gap)
    random.shuffle(eids); now = datetime.now(); queued = 0
    for eid, ts in zip(eids, timestamps):
        with get_db() as conn:
            conn.execute("UPDATE emails SET send_time=?, send_time_str=? WHERE id=?",
                         (ts.isoformat(), ts.strftime("%Y-%m-%d %H:%M"), eid))
        if ts <= now:
            threading.Thread(target=_dispatch, args=(eid,), daemon=True).start()
        else:
            scheduler.add_job(_dispatch, trigger="date", run_date=ts,
                              args=[eid], id=f"email_{eid}", replace_existing=True)
            with get_db() as conn:
                conn.execute("UPDATE emails SET status='scheduled' WHERE id=? AND status NOT IN ('sent','sending')", (eid,))
            queued += 1
    return jsonify({"count": len(eids), "queued": queued,
                    "start": start.isoformat(), "end": end.isoformat(), "business_hours": business_hours})


# ── Accounts ───────────────────────────────────────────────────────────────────

@app.route("/api/accounts")
def list_accounts():
    return jsonify({"accounts": get_sending_accounts(), "selected": selected_account_email})


@app.route("/api/set-account", methods=["POST"])
def set_account():
    global selected_account_email
    selected_account_email = (request.json or {}).get("email", "")
    return jsonify({"success": True, "selected": selected_account_email})


@app.route("/api/smtp-password", methods=["POST"])
def smtp_password():
    data = request.json or {}; email = data.get("email", "").strip(); password = data.get("password", "").strip()
    if not email: return jsonify({"error": "Email required"}), 400
    if password: _kc_set(email, password); return jsonify({"ok": True, "configured": True})
    else: _kc_del(email); return jsonify({"ok": True, "configured": False})


@app.route("/api/smtp-status")
def smtp_status():
    email = selected_account_email
    return jsonify({"email": email, "configured": bool(email and _kc_get(email))})


# ── Upload ─────────────────────────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files: return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if not file.filename.lower().endswith(".csv"): return jsonify({"error": "Only CSV files supported"}), 400
    batch_id = request.form.get("batch_id"); batch_name = (request.form.get("batch_name") or "").strip()
    try:
        content = file.read().decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(content))
        df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    except Exception as e:
        return jsonify({"error": f"Could not parse CSV: {e}"}), 400
    missing = {"to", "subject", "body"} - set(df.columns)
    if missing: return jsonify({"error": f"Missing columns: {', '.join(sorted(missing))}"}), 400
    now = datetime.now(); cols = set(df.columns)
    with get_db() as conn:
        if not batch_id:
            name = batch_name or f"Import {now.strftime('%b %d %H:%M')}"
            cur = conn.execute("INSERT INTO batches (name, created_at) VALUES (?,?)", (name, now.isoformat()))
            batch_id = cur.lastrowid
        else:
            batch_id = int(batch_id)
        br = conn.execute("SELECT professor_mode FROM batches WHERE id=?", (batch_id,)).fetchone()
        professor_mode = bool(br["professor_mode"]) if br else False
        count = 0
        for _, row in df.iterrows():
            rec = _make_record(row, cols, now, batch_id, professor_mode)
            dup = _check_duplicate(conn, rec["to_addr"])
            cw = 1 if _has_content_warning(rec["body"], rec["subject"]) else 0
            cur = conn.execute(
                "INSERT INTO emails (batch_id, to_addr, cc, bcc, subject, body, attachments,"
                "send_time, send_time_str, status, duplicate_warning, content_warning, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["batch_id"], rec["to_addr"], rec["cc"], rec["bcc"], rec["subject"], rec["body"],
                 rec["attachments"], rec["send_time"], rec["send_time_str"],
                 rec["status"], 1 if dup else 0, cw, rec["created_at"])
            )
            _add_history(conn, cur.lastrowid, "created", "Imported from CSV"); count += 1
    return jsonify({"success": True, "count": count, "batch_id": batch_id, "professor_mode": professor_mode})


@app.route("/api/export/sent-addresses")
def export_sent_addresses():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT email_addr FROM sent_address_log ORDER BY first_sent_at DESC"
        ).fetchall()
    lines = [r["email_addr"] for r in rows]
    buf = io.BytesIO("\n".join(lines).encode())
    return send_file(buf, mimetype="text/plain", as_attachment=True,
                     download_name="sent_addresses.txt")


@app.route("/api/export/all-addresses")
def export_all_addresses():
    batch_id = request.args.get("batch_id", "")
    with get_db() as conn:
        if batch_id:
            rows = conn.execute(
                "SELECT DISTINCT to_addr FROM emails WHERE batch_id=? AND to_addr!='' ORDER BY to_addr",
                (int(batch_id),)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT to_addr FROM emails WHERE to_addr!='' ORDER BY to_addr"
            ).fetchall()
    lines = [r["to_addr"] for r in rows]
    buf = io.BytesIO("\n".join(lines).encode())
    return send_file(buf, mimetype="text/plain", as_attachment=True,
                     download_name="all_addresses.txt")


@app.route("/api/sample-csv")
def sample_csv():
    now = datetime.now()
    lines = [
        "to,subject,body,send_time,cc,bcc,attachments",
        f'professor@university.edu,Research Opportunity Inquiry,"Dear Professor Smith,\n\nI am a junior at Collin College majoring in Computer Science interested in your ML research.\n\nBest regards,\nAryan Patel",{now.strftime("%Y-%m-%d %H:%M")},,,',
        f'prof2@university.edu,Undergraduate Research Inquiry,"Dear Professor Jones,\n\nI came across your paper on distributed systems and found it fascinating.\n\nBest,\nAryan",{now.strftime("%Y-%m-%d %H:%M")},,,',
    ]
    buf = io.BytesIO("\n".join(lines).encode())
    return send_file(buf, mimetype="text/csv", as_attachment=True, download_name="sample_emails.csv")


# ── Duplicate resolution API ───────────────────────────────────────────────────

@app.route("/api/duplicate-groups")
def get_duplicate_groups():
    """Return groups of emails sharing the same to_addr (2+ non-skipped/invalid emails)."""
    batch_id = request.args.get("batch_id", "")
    with get_db() as conn:
        params: list = []
        batch_clause = ""
        if batch_id:
            batch_clause = "AND batch_id=?"
            params.append(int(batch_id))

        # Find addresses that appear in 2+ active emails
        addrs = conn.execute(
            f"SELECT LOWER(to_addr) AS addr FROM emails "
            f"WHERE status NOT IN ('invalid','skipped') {batch_clause} "
            f"GROUP BY LOWER(to_addr) HAVING COUNT(*) > 1",
            params
        ).fetchall()

        result = []
        for addr_row in addrs:
            addr = addr_row["addr"]
            e_params = [addr] + (params[:] if params else [])
            emails = conn.execute(
                f"SELECT e.*, b.name AS batch_name FROM emails e "
                f"LEFT JOIN batches b ON b.id=e.batch_id "
                f"WHERE LOWER(e.to_addr)=? {batch_clause} "
                f"AND e.status NOT IN ('invalid','skipped') "
                f"ORDER BY e.created_at",
                e_params
            ).fetchall()
            if len(emails) >= 2:
                result.append({
                    "to_addr": emails[0]["to_addr"],
                    "emails": [row_to_dict(r) for r in emails]
                })

    return jsonify(result)


@app.route("/api/resolve-duplicate-group", methods=["POST"])
def resolve_duplicate_group():
    """Resolve one duplicate group: keep_one, keep_all, or pick_random."""
    data = request.json or {}
    to_addr = (data.get("to_addr") or "").strip()
    action = data.get("action", "")
    keep_id = data.get("keep_id")
    batch_id = data.get("batch_id")

    if not to_addr or action not in ("keep_one", "keep_all", "pick_random"):
        return jsonify({"error": "Invalid request"}), 400

    with get_db() as conn:
        params: list = [to_addr]
        batch_clause = ""
        if batch_id:
            batch_clause = "AND batch_id=?"
            params.append(int(batch_id))

        rows = conn.execute(
            f"SELECT id, status FROM emails "
            f"WHERE LOWER(to_addr)=LOWER(?) {batch_clause} "
            f"AND status NOT IN ('invalid','skipped')",
            params
        ).fetchall()

        if action == "keep_all":
            for row in rows:
                conn.execute("UPDATE emails SET duplicate_warning=2 WHERE id=?", (row["id"],))

        elif action == "keep_one":
            if not keep_id:
                return jsonify({"error": "keep_id required"}), 400
            for row in rows:
                if row["id"] == int(keep_id):
                    conn.execute("UPDATE emails SET duplicate_warning=2 WHERE id=?", (row["id"],))
                elif row["status"] not in ("sent", "sending"):
                    conn.execute(
                        "UPDATE emails SET status='skipped', duplicate_warning=2 WHERE id=?",
                        (row["id"],)
                    )
                    try:
                        scheduler.remove_job(f"email_{row['id']}")
                    except Exception:
                        pass

        elif action == "pick_random":
            unsent = [r for r in rows if r["status"] not in ("sent", "sending")]
            if unsent:
                kept = random.choice(unsent)
                for row in rows:
                    if row["id"] == kept["id"]:
                        conn.execute("UPDATE emails SET duplicate_warning=2 WHERE id=?", (row["id"],))
                    elif row["status"] not in ("sent", "sending"):
                        conn.execute(
                            "UPDATE emails SET status='skipped', duplicate_warning=2 WHERE id=?",
                            (row["id"],)
                        )
                        try:
                            scheduler.remove_job(f"email_{row['id']}")
                        except Exception:
                            pass
            else:
                for row in rows:
                    conn.execute("UPDATE emails SET duplicate_warning=2 WHERE id=?", (row["id"],))

    return jsonify({"success": True})


@app.route("/api/resolve-all-duplicates-random", methods=["POST"])
def resolve_all_duplicates_random():
    """Pick one random unsent email per duplicate group and skip the rest."""
    data = request.json or {}
    batch_id = data.get("batch_id")
    with get_db() as conn:
        params: list = []
        batch_clause = ""
        if batch_id:
            batch_clause = "AND batch_id=?"
            params.append(int(batch_id))

        addrs = conn.execute(
            f"SELECT LOWER(to_addr) AS addr FROM emails "
            f"WHERE status NOT IN ('invalid','skipped') {batch_clause} "
            f"GROUP BY LOWER(to_addr) HAVING COUNT(*) > 1",
            params
        ).fetchall()

        resolved = 0
        for addr_row in addrs:
            addr = addr_row["addr"]
            e_params = [addr] + (params[:] if params else [])
            rows = conn.execute(
                f"SELECT id, status FROM emails "
                f"WHERE LOWER(to_addr)=? {batch_clause} "
                f"AND status NOT IN ('invalid','skipped','failed')",
                e_params
            ).fetchall()
            if len(rows) < 2:
                continue
            unsent = [r for r in rows if r["status"] not in ("sent", "sending")]
            if unsent:
                kept = random.choice(unsent)
                for row in rows:
                    if row["id"] == kept["id"]:
                        conn.execute("UPDATE emails SET duplicate_warning=2 WHERE id=?", (row["id"],))
                    elif row["status"] not in ("sent", "sending"):
                        conn.execute(
                            "UPDATE emails SET status='skipped', duplicate_warning=2 WHERE id=?",
                            (row["id"],)
                        )
                        try:
                            scheduler.remove_job(f"email_{row['id']}")
                        except Exception:
                            pass
            else:
                for row in rows:
                    conn.execute("UPDATE emails SET duplicate_warning=2 WHERE id=?", (row["id"],))
            resolved += 1

    return jsonify({"success": True, "resolved": resolved})


# ── Auto-reschedule settings API ───────────────────────────────────────────────

@app.route("/api/auto-reschedule-settings", methods=["GET"])
def get_ar_settings_route():
    return jsonify(_get_ar_settings())


@app.route("/api/auto-reschedule-settings", methods=["POST"])
def post_ar_settings_route():
    data = request.json or {}
    settings = {
        "enabled": bool(data.get("enabled", False)),
        "window_minutes": max(5, int(data.get("window_minutes", 60))),
    }
    _save_ar_settings(settings)
    return jsonify({"success": True, "settings": settings})


@app.route("/api/reply-check-settings", methods=["GET"])
def get_reply_settings_route():
    return jsonify(_get_reply_settings())


@app.route("/api/reply-check-settings", methods=["POST"])
def post_reply_settings_route():
    data = request.json or {}
    settings = {
        "enabled": bool(data.get("enabled", True)),
        "interval_minutes": max(5, int(data.get("interval_minutes", 60))),
    }
    _save_reply_settings(settings)
    _apply_reply_schedule()
    return jsonify({"success": True, "settings": settings})


# ── Updates ──────────────────────────────────────────────────────────────────────

@app.route("/api/version")
def api_version():
    """Current vs. latest version. Cached so the GitHub API isn't hammered."""
    return jsonify(_compute_update(force=False))


@app.route("/api/check-update", methods=["POST"])
def api_check_update():
    """Force a fresh check against GitHub, bypassing the cache."""
    return jsonify(_compute_update(force=True))


@app.route("/api/update", methods=["POST"])
def api_update():
    """Pull the latest code (fast-forward only) and restart in place to apply it."""
    if not _is_git_repo():
        return jsonify({"success": False, "error": "Not a git checkout — update manually."}), 400
    before = _local_commit()
    fcode, _, ferr = _git("fetch", "origin", GITHUB_BRANCH)
    if fcode != 0:
        return jsonify({"success": False, "error": ferr or "git fetch failed"}), 500
    mcode, mout, merr = _git("merge", "--ff-only", f"origin/{GITHUB_BRANCH}")
    if mcode != 0:
        msg = merr or mout or "git merge failed"
        if "local changes" in msg.lower() or "overwritten" in msg.lower():
            msg = "You have local changes that would be overwritten. Commit or discard them first."
        return jsonify({"success": False, "error": msg}), 409
    after = _local_commit()
    updated = bool(after and after != before)
    reqs_changed = False
    if updated:
        dcode, dout, _ = _git("diff", "--name-only", before, after)
        reqs_changed = dcode == 0 and "requirements.txt" in dout.split("\n")
    _compute_update(force=True)  # refresh cache so the banner clears
    if updated:
        _restart_process(install_deps=reqs_changed)
    return jsonify({"success": True, "updated": updated, "restarting": updated,
                    "current": after, "current_short": after[:7] if after else None})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Stop the app: shut down the scheduler and exit the process.

    Triggered by the 'Stop App' button in the UI. The actual exit happens in a
    short-delayed background thread so this HTTP response can flush to the
    browser first.
    """
    def _stop():
        import time
        time.sleep(0.4)
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return jsonify({"success": True})


if __name__ == "__main__":
    print("\n  Email Scheduler v3")
    print("  Persistent SQLite storage: scheduler.db")
    print("  Running at http://127.0.0.1:5001\n")
    app.run(debug=False, port=5001, host="127.0.0.1")
