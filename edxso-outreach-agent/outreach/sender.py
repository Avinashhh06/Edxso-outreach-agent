"""Stage 6+7: Review gate, sending layer, outreach log.

Safety design
  * Nothing is sent unless the message is APPROVED in the review step.
  * Default SEND_MODE=dry_run: emails are only simulated (logged, never transmitted).
  * live mode with TEST_RECIPIENT set redirects EVERY email to that address (safe rehearsal).
  * Duplicate prevention: one real send per influencer AND per recipient address.
  * Instagram DMs are never automated (platform rules). They go to a manual queue.
"""
from __future__ import annotations

import json
import smtplib
import sqlite3
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

from .config import Settings
from .llm import ConfigError
from .utils import is_valid_email, now_iso


# ------------------------------------------------------------------ logging
def log_event(conn: sqlite3.Connection, inf_id: str, channel: str, recipient: str, status: str, mode: str, detail: str = "") -> None:
    with conn:
        conn.execute(
            "INSERT INTO outreach_log (influencer_id,channel,recipient,status,mode,detail,created_at) VALUES (?,?,?,?,?,?,?)",
            (inf_id, channel, recipient, status, mode, detail, now_iso()),
        )


def _last_status(conn, inf_id: str, channel: str) -> str | None:
    r = conn.execute("SELECT status FROM outreach_log WHERE influencer_id=? AND channel=? ORDER BY id DESC LIMIT 1", (inf_id, channel)).fetchone()
    return r[0] if r else None


def _log_once(conn, inf_id, channel, recipient, status, mode, detail=""):
    """Idempotent log for skip-type events so re-runs do not spam the log."""
    if _last_status(conn, inf_id, channel) != status:
        log_event(conn, inf_id, channel, recipient, status, mode, detail)


# ------------------------------------------------------------------ review gate
def set_review(conn: sqlite3.Connection, status: str, ids: list[str] | None = None) -> int:
    if status not in {"approved", "rejected", "pending"}:
        raise ValueError("status must be approved|rejected|pending")
    q = "UPDATE messages SET review_status=?, reviewed_at=? " + ("WHERE influencer_id IN (%s)" % ",".join("?" * len(ids)) if ids else "")
    with conn:
        cur = conn.execute(q, [status, now_iso(), *(ids or [])])
    return cur.rowcount


# ------------------------------------------------------------------ email
def _already_sent(conn, inf_id: str, email: str, dry: bool) -> str | None:
    blocking = ("SENT", "SIMULATED") if dry else ("SENT",)
    q = ",".join("?" * len(blocking))
    if conn.execute(f"SELECT 1 FROM outreach_log WHERE channel='email' AND influencer_id=? AND status IN ({q})", (inf_id, *blocking)).fetchone():
        return "this influencer already contacted"
    if conn.execute(f"SELECT 1 FROM outreach_log WHERE channel='email' AND lower(recipient)=? AND status IN ({q})", (email.lower(), *blocking)).fetchone():
        return "this email address already contacted"
    return None


def _smtp_send(s: Settings, to_addr: str, subject: str, body: str) -> None:
    missing = [n for n, v in [("SMTP_USER", s.smtp_user), ("SMTP_APP_PASSWORD", s.smtp_password), ("FROM_EMAIL", s.from_email)] if not v]
    if missing:
        raise ConfigError(f"Live sending needs in .env: {', '.join(missing)}")
    msg = EmailMessage()
    msg["From"] = formataddr((s.section("company").get("sender_name", ""), s.from_email))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)


def send_emails(conn: sqlite3.Connection, s: Settings, limit: int | None = None, sleep=time.sleep) -> dict:
    sc = s.section("sending")
    live = s.send_mode == "live"
    redirect = live and bool(s.test_recipient)
    mode = "live_test" if redirect else ("live" if live else "dry_run")
    if redirect and not is_valid_email(s.test_recipient):
        raise ConfigError("TEST_RECIPIENT is not a valid email address")
    stats = {"mode": mode, "sent": 0, "simulated": 0, "skipped_no_email": 0, "skipped_duplicate": 0,
             "skipped_caution": 0, "skipped_not_approved": 0, "failed": 0, "stopped_by_cap": False}

    rows = conn.execute(
        "SELECT m.*, e.record_json FROM messages m JOIN enriched e USING(influencer_id) ORDER BY m.influencer_id"
    ).fetchall()
    today = datetime.now().date().isoformat()
    sent_today = conn.execute("SELECT COUNT(*) FROM outreach_log WHERE channel='email' AND status='SENT' AND created_at LIKE ?", (today + "%",)).fetchone()[0]
    attempted = 0
    for r in rows:
        rec = json.loads(r["record_json"])
        iid, email = r["influencer_id"], rec["contact_email"]
        if r["review_status"] != "approved":
            stats["skipped_not_approved"] += 1
            continue
        if not is_valid_email(email):
            stats["skipped_no_email"] += 1
            _log_once(conn, iid, "email", "", "SKIPPED_NO_EMAIL", mode, "contact email Not Found; use manual DM")
            continue
        if sc.get("block_caution_emails", True) and rec.get("email_caution"):
            stats["skipped_caution"] += 1
            _log_once(conn, iid, "email", email, "SKIPPED_CAUTION", mode, rec["email_caution"])
            continue
        if not redirect:
            why = _already_sent(conn, iid, email, dry=not live)
            if why:
                stats["skipped_duplicate"] += 1
                continue
        if limit is not None and attempted >= limit:
            break
        if live and not redirect and sent_today >= sc.get("daily_cap", 20):
            stats["stopped_by_cap"] = True
            break
        attempted += 1
        to_addr = s.test_recipient if redirect else email
        subject = r["email_subject"]
        body = r["email_body"]
        if not live:
            log_event(conn, iid, "email", email, "SIMULATED", mode, f"dry run; subject={subject!r}")
            stats["simulated"] += 1
            continue
        try:
            _smtp_send(s, to_addr, ("[TEST] " if redirect else "") + subject, body)
        except ConfigError:
            raise
        except (smtplib.SMTPException, OSError) as exc:
            log_event(conn, iid, "email", to_addr, "FAILED", mode, str(exc)[:300])
            stats["failed"] += 1
            continue
        log_event(conn, iid, "email", to_addr, "TEST_SENT" if redirect else "SENT", mode, f"intended_recipient={email}" if redirect else "")
        stats["sent"] += 1
        if not redirect:
            sent_today += 1
        sleep(sc.get("delay_seconds", 3))
    return stats


# ------------------------------------------------------------------ Instagram DM (manual / simulated workflow)
def queue_dms(conn: sqlite3.Connection) -> int:
    """Approved DMs go to a manual queue. We never automate Instagram DMs."""
    n = 0
    for r in conn.execute("SELECT m.influencer_id, e.record_json FROM messages m JOIN enriched e USING(influencer_id) WHERE m.review_status='approved'").fetchall():
        rec = json.loads(r["record_json"])
        if _last_status(conn, r["influencer_id"], "instagram_dm") is None:
            log_event(conn, r["influencer_id"], "instagram_dm", rec["profile_url"], "MANUAL_PENDING", "manual", "send by hand from the brand account")
            n += 1
    return n


def dm_queue(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT m.influencer_id, m.dm_text, e.record_json FROM messages m JOIN enriched e USING(influencer_id) WHERE m.review_status='approved' ORDER BY m.influencer_id"
    ):
        rec = json.loads(r["record_json"])
        out.append({"influencer_id": r["influencer_id"], "name": rec["name"], "profile_url": rec["profile_url"],
                    "dm_text": r["dm_text"], "status": _last_status(conn, r["influencer_id"], "instagram_dm") or "NOT_QUEUED"})
    return out


def mark_dm_sent(conn: sqlite3.Connection, inf_id: str) -> None:
    if not conn.execute("SELECT 1 FROM messages WHERE influencer_id=? AND review_status='approved'", (inf_id,)).fetchone():
        raise ValueError(f"{inf_id}: no approved message")
    log_event(conn, inf_id, "instagram_dm", "", "MANUAL_SENT", "manual", "marked sent by operator")
