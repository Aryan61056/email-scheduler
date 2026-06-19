#!/usr/bin/env python3
"""Outlook Email Scheduler — sends emails via Microsoft Outlook for Mac using AppleScript."""

import os
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from typing import List

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler


# ── AppleScript helpers ────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape a string for embedding inside AppleScript double quotes."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _as_body(text: str) -> str:
    """Convert a multiline Python string to an AppleScript string expression."""
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return " & return & ".join(f'"{_esc(line)}"' for line in lines)


def _parse_addrs(raw) -> List[str]:
    """Parse a comma-separated address field, returning an empty list for blank/NaN."""
    if raw is None:
        return []
    try:
        if pd.isna(raw):
            return []
    except (TypeError, ValueError):
        pass
    return [a.strip() for a in str(raw).split(",") if a.strip()]


# ── Email sending ──────────────────────────────────────────────────────────────

def send_via_outlook(to, subject, body, cc="", bcc="", attachments=""):
    """Send an email through Microsoft Outlook for Mac via AppleScript."""
    to_list = _parse_addrs(to)
    if not to_list:
        raise ValueError("No recipients specified")

    cc_list = _parse_addrs(cc)
    bcc_list = _parse_addrs(bcc)
    attach_list = _parse_addrs(attachments)

    lines = [
        'tell application "Microsoft Outlook"',
        f'    set msgBody to {_as_body(body)}',
        f'    set msg to make new outgoing message with properties'
        f' {{subject:"{_esc(str(subject))}", content:msgBody}}',
    ]

    for addr in to_list:
        lines.append(
            f'    make new recipient at msg with properties'
            f' {{email address:{{address:"{_esc(addr)}"}}}}'
        )
    for addr in cc_list:
        lines.append(
            f'    make new cc recipient at msg with properties'
            f' {{email address:{{address:"{_esc(addr)}"}}}}'
        )
    for addr in bcc_list:
        lines.append(
            f'    make new bcc recipient at msg with properties'
            f' {{email address:{{address:"{_esc(addr)}"}}}}'
        )
    for path in attach_list:
        if os.path.exists(path):
            lines.append(
                f'    make new attachment at msg with properties'
                f' {{file:POSIX file "{_esc(path)}"}}'
            )

    lines += ["    send msg", "end tell"]
    script = "\n".join(lines)

    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Unknown AppleScript error")


def check_outlook_available() -> bool:
    """Return True if Microsoft Outlook is installed and responding."""
    result = subprocess.run(
        ["osascript", "-e", 'tell application "Microsoft Outlook" to return name'],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0


# ── Constants ──────────────────────────────────────────────────────────────────

COLS = ("to", "subject", "send_time", "status")
COL_LABELS = {"to": "To", "subject": "Subject", "send_time": "Send Time", "status": "Status"}
COL_WIDTHS = {"to": 240, "subject": 290, "send_time": 150, "status": 110}
BLUE = "#2b579a"
WHITE = "#ffffff"
BG = "#f5f5f5"

SAMPLE_ROWS = [
    {
        "to": "recipient@example.com",
        "cc": "",
        "bcc": "",
        "subject": "Hello from Scheduler",
        "body": "Hi there,\n\nThis is a scheduled email.\n\nBest,\nAryan",
        "send_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "attachments": "",
    },
    {
        "to": "person1@example.com,person2@example.com",
        "cc": "manager@example.com",
        "bcc": "",
        "subject": "Team Update",
        "body": "Hi team,\n\nJust a quick update from the scheduler.",
        "send_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "attachments": "",
    },
]


# ── Main Application ───────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Outlook Email Scheduler")
        self.geometry("860x520")
        self.configure(bg=BG)
        self.minsize(700, 380)

        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.start()
        self.rows: List[dict] = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._check_outlook)

    # ── Startup check ──────────────────────────────────────────────────────────

    def _check_outlook(self):
        try:
            ok = check_outlook_available()
        except Exception:
            ok = False
        if not ok:
            messagebox.showwarning(
                "Outlook Not Found",
                "Microsoft Outlook does not appear to be installed or running.\n\n"
                "Please open Outlook and make sure you're logged in, then restart this app.",
            )

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_toolbar()
        self._build_table()
        self._build_statusbar()

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BLUE, pady=10)
        bar.pack(fill="x")

        tk.Label(
            bar, text="Outlook Email Scheduler",
            font=("Helvetica", 15, "bold"), bg=BLUE, fg=WHITE,
        ).pack(side="left", padx=16)

        for label, cmd in [("Sample CSV", self._save_sample), ("Load CSV", self._load_csv)]:
            tk.Button(
                bar, text=label, command=cmd,
                bg=WHITE, fg=BLUE, font=("Helvetica", 11),
                relief="flat", padx=10, pady=3, cursor="hand2",
                activebackground="#e8eaf6", activeforeground=BLUE,
            ).pack(side="right", padx=6)

    def _build_table(self):
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="both", expand=True, padx=14, pady=10)

        style = ttk.Style()
        style.configure("Treeview", font=("Helvetica", 11), rowheight=26)
        style.configure("Treeview.Heading", font=("Helvetica", 11, "bold"))

        self.tree = ttk.Treeview(frame, columns=COLS, show="headings", selectmode="extended")
        for c in COLS:
            self.tree.heading(c, text=COL_LABELS[c])
            self.tree.column(c, width=COL_WIDTHS[c], anchor="w", minwidth=80)

        self.tree.tag_configure("pending", foreground="#444444")
        self.tree.tag_configure("overdue", foreground="#9a3800")
        self.tree.tag_configure("sending", foreground="#0969da")
        self.tree.tag_configure("sent", foreground="#1a7f37")
        self.tree.tag_configure("failed", foreground="#cf222e")
        self.tree.tag_configure("invalid", foreground="#888888")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        # Right-click / Ctrl+click context menu
        self.ctx_menu = tk.Menu(self, tearoff=0)
        self.ctx_menu.add_command(label="Send Now", command=self._send_selected_now)
        self.ctx_menu.add_command(label="View Details", command=self._view_details)
        self.tree.bind("<Button-2>", self._show_ctx)
        self.tree.bind("<Control-Button-1>", self._show_ctx)

    def _build_statusbar(self):
        bar = tk.Frame(self, bg="#e0e0e0", pady=7)
        bar.pack(fill="x", side="bottom")

        self.status_var = tk.StringVar(value="Load a CSV file to get started.")
        tk.Label(
            bar, textvariable=self.status_var,
            bg="#e0e0e0", font=("Helvetica", 10), anchor="w",
        ).pack(side="left", padx=14)

        self.sched_btn = tk.Button(
            bar, text="Schedule All", command=self._schedule_all,
            bg=BLUE, fg=WHITE, font=("Helvetica", 11, "bold"),
            relief="flat", padx=14, pady=3, cursor="hand2",
            activebackground="#1e3f7a", activeforeground=WHITE,
            state="disabled",
        )
        self.sched_btn.pack(side="right", padx=14)

    # ── CSV loading ────────────────────────────────────────────────────────────

    def _load_csv(self):
        path = filedialog.askopenfilename(
            title="Select email schedule CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            df = pd.read_csv(path)
            df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
        except Exception as e:
            messagebox.showerror("Load Error", f"Could not read CSV:\n{e}")
            return

        required = {"to", "subject", "body", "send_time"}
        missing = required - set(df.columns)
        if missing:
            messagebox.showerror(
                "Missing Columns",
                f"CSV must include: {', '.join(sorted(missing))}\n\n"
                "Click 'Sample CSV' to see the expected format.",
            )
            return

        self.tree.delete(*self.tree.get_children())
        self.rows = []
        now = datetime.now()

        for _, row in df.iterrows():
            try:
                send_time = pd.to_datetime(row["send_time"])
                status = "Pending" if send_time > now else "Overdue"
                time_str = send_time.strftime("%Y-%m-%d %H:%M")
            except Exception:
                send_time = None
                status = "Invalid time"
                time_str = str(row.get("send_time", ""))

            record = {
                "to": str(row.get("to", "")),
                "cc": str(row.get("cc", "")) if "cc" in df.columns else "",
                "bcc": str(row.get("bcc", "")) if "bcc" in df.columns else "",
                "subject": str(row.get("subject", "")),
                "body": str(row.get("body", "")),
                "attachments": str(row.get("attachments", "")) if "attachments" in df.columns else "",
                "send_time": send_time,
                "status": status,
            }
            self.rows.append(record)

            idx = len(self.rows) - 1
            tag = "overdue" if status == "Overdue" else ("invalid" if send_time is None else "pending")
            self.tree.insert("", "end", iid=str(idx), values=(
                record["to"], record["subject"], time_str, status,
            ), tags=(tag,))

        n = len(self.rows)
        self.status_var.set(f"Loaded {n} email{'s' if n != 1 else ''} from {os.path.basename(path)}.")
        self.sched_btn.config(state="normal")

    # ── Scheduling ─────────────────────────────────────────────────────────────

    def _schedule_all(self):
        now = datetime.now()
        queued = immediate = skipped = 0

        for i, record in enumerate(self.rows):
            if record["status"] in ("Sent", "Sending"):
                continue
            send_time = record["send_time"]
            if send_time is None:
                skipped += 1
                continue
            if send_time <= now:
                self._dispatch(i)
                immediate += 1
            else:
                self.scheduler.add_job(
                    self._dispatch, trigger="date", run_date=send_time,
                    args=[i], id=f"row_{i}", replace_existing=True,
                )
                queued += 1

        parts = []
        if queued:
            parts.append(f"{queued} scheduled")
        if immediate:
            parts.append(f"{immediate} sending now")
        if skipped:
            parts.append(f"{skipped} skipped (invalid time)")
        self.status_var.set(", ".join(parts) + "." if parts else "Nothing to schedule.")
        self.sched_btn.config(text="Reschedule")

    def _dispatch(self, idx: int):
        record = self.rows[idx]
        self._set_status(idx, "Sending…", "sending")
        try:
            send_via_outlook(
                to=record["to"],
                subject=record["subject"],
                body=record["body"],
                cc=record.get("cc", ""),
                bcc=record.get("bcc", ""),
                attachments=record.get("attachments", ""),
            )
            record["status"] = "Sent"
            self._set_status(idx, "Sent", "sent")
        except Exception as exc:
            record["status"] = "Failed"
            self._set_status(idx, "Failed", "failed")
            self.after(0, lambda e=str(exc), r=record: messagebox.showerror(
                "Send Failed", f"Could not send to {r['to']}:\n\n{e}"
            ))

    def _set_status(self, idx: int, status: str, tag: str):
        def _do():
            iid = str(idx)
            if self.tree.exists(iid):
                vals = list(self.tree.item(iid, "values"))
                vals[3] = status
                self.tree.item(iid, values=vals, tags=(tag,))
        self.after(0, _do)

    # ── Context menu actions ───────────────────────────────────────────────────

    def _show_ctx(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.ctx_menu.post(event.x_root, event.y_root)

    def _send_selected_now(self):
        for iid in self.tree.selection():
            self._dispatch(int(iid))

    def _view_details(self):
        selected = self.tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        r = self.rows[idx]

        win = tk.Toplevel(self)
        win.title(f"Email {idx + 1} — Details")
        win.geometry("520x400")
        win.configure(bg=BG)

        fields = [
            ("To", r["to"]),
            ("CC", r.get("cc", "") or "—"),
            ("BCC", r.get("bcc", "") or "—"),
            ("Subject", r["subject"]),
            ("Send Time", r["send_time"].strftime("%Y-%m-%d %H:%M") if r["send_time"] else "Invalid"),
            ("Status", r["status"]),
            ("Attachments", r.get("attachments", "") or "—"),
        ]

        for label, value in fields:
            row_frame = tk.Frame(win, bg=BG)
            row_frame.pack(fill="x", padx=16, pady=2)
            tk.Label(row_frame, text=f"{label}:", font=("Helvetica", 10, "bold"),
                     bg=BG, width=12, anchor="w").pack(side="left")
            tk.Label(row_frame, text=value, font=("Helvetica", 10),
                     bg=BG, anchor="w", wraplength=380, justify="left").pack(side="left")

        tk.Label(win, text="Body:", font=("Helvetica", 10, "bold"), bg=BG, anchor="w").pack(
            fill="x", padx=16, pady=(8, 2))
        body_text = tk.Text(win, font=("Helvetica", 10), height=8, wrap="word",
                            relief="solid", bd=1, padx=6, pady=6)
        body_text.insert("1.0", r["body"])
        body_text.config(state="disabled")
        body_text.pack(fill="both", expand=True, padx=16, pady=(0, 16))

    # ── Sample CSV ─────────────────────────────────────────────────────────────

    def _save_sample(self):
        path = filedialog.asksaveasfilename(
            title="Save sample CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="sample_emails.csv",
        )
        if not path:
            return
        pd.DataFrame(SAMPLE_ROWS).to_csv(path, index=False)
        messagebox.showinfo("Sample Saved", f"Sample CSV saved to:\n{path}\n\n"
                            "Open it in Excel or Numbers to edit.")

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def _on_close(self):
        self.scheduler.shutdown(wait=False)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
