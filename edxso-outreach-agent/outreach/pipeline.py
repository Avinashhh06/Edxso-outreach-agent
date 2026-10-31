"""Autonomous orchestrator: Discovery -> Collection -> Filtering -> Enrichment -> Personalization -> Review -> Sending -> Tracking.

Each stage is recorded in `stage_runs`. A failing stage stops the run with a clear message
(later stages depend on earlier ones); all stages are idempotent so you can fix and re-run.
"""
from __future__ import annotations

import copy
import logging
import sqlite3
from typing import Callable

from . import discovery, enrichment, exports, filtering, personalization, sender
from .config import Settings
from .utils import now_iso

log = logging.getLogger("agent")


def _record(conn, stage, status, detail, started):
    with conn:
        conn.execute("INSERT INTO stage_runs (stage,status,detail,started_at,finished_at) VALUES (?,?,?,?,?)", (stage, status, str(detail)[:500], started, now_iso()))


def run_pipeline(conn: sqlite3.Connection, s: Settings, *, auto_approve: bool = False, send: bool = False,
                 force_messages: bool = False, categories: list[str] | None = None, progress: Callable[[str], None] = print) -> dict:
    cfg = copy.deepcopy(s.cfg)
    if categories:
        cfg["filters"]["active_categories"] = categories
    s_run = Settings(**{**s.__dict__, "cfg": cfg})
    summary: dict = {}
    stages: list[tuple[str, Callable[[], dict]]] = [
        ("discovery", lambda: discovery.run(conn, s.data_csv)),
        ("filtering", lambda: filtering.run(conn, cfg)),
        ("enrichment", lambda: enrichment.run(conn)),
        ("personalization", lambda: personalization.run(conn, s_run, force=force_messages)),
    ]
    for name, fn in stages:
        started = now_iso()
        progress(f"[{name}] running...")
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001 - report then stop; stages are re-runnable
            _record(conn, name, "FAILED", exc, started)
            progress(f"[{name}] FAILED: {exc}")
            summary[name] = {"error": str(exc)}
            summary["stopped_at"] = name
            return summary
        _record(conn, name, "OK", res, started)
        summary[name] = res
        progress(f"[{name}] {res}")

    if auto_approve:
        n = sender.set_review(conn, "approved")
        summary["review"] = {"auto_approved": n}
        progress(f"[review] auto-approved {n} messages (use `review` to approve manually instead)")
    else:
        pending = conn.execute("SELECT COUNT(*) FROM messages WHERE review_status='pending'").fetchone()[0]
        summary["review"] = {"pending": pending}
        progress(f"[review] {pending} messages waiting for approval -> `python main.py review --list` / dashboard")

    if send:
        started = now_iso()
        try:
            res = sender.send_emails(conn, s_run)
            res["dm_queued"] = sender.queue_dms(conn)
            _record(conn, "sending", "OK", res, started)
        except Exception as exc:  # noqa: BLE001
            _record(conn, "sending", "FAILED", exc, started)
            summary["sending"] = {"error": str(exc)}
            progress(f"[sending] FAILED: {exc}")
        else:
            summary["sending"] = res
            progress(f"[sending] {res}")

    files = exports.export_all(conn, s.out_dir)
    summary["exports"] = [str(f.name) for f in files]
    progress(f"[tracking] exported {len(files)} files to {s.out_dir}")
    return summary
