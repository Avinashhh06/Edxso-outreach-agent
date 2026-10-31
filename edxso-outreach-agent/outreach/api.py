"""FastAPI layer + review dashboard.   Start:  python main.py serve   ->  http://127.0.0.1:8000"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, exports, llm, sender
from .config import load_settings
from .pipeline import run_pipeline

app = FastAPI(title="EDXSO Outreach Agent", version="1.0.0")
SETTINGS = load_settings()
STATIC = Path(__file__).parent / "static"


def get_conn():
    conn = db.connect(SETTINGS.db_path)
    try:
        yield conn
    finally:
        conn.close()


class RunBody(BaseModel):
    auto_approve: bool = False
    categories: list[str] | None = None
    force_messages: bool = False


class ReviewBody(BaseModel):
    status: str  # approved | rejected | pending


class SendBody(BaseModel):
    limit: int | None = None


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/api/health")
def health():
    return {"ok": True, "llm_provider": SETTINGS.llm_provider, "send_mode": SETTINGS.send_mode}


@app.get("/api/status")
def status(conn=Depends(get_conn)):
    q = lambda sql: [tuple(r) for r in conn.execute(sql).fetchall()]  # noqa: E731
    return {
        "influencers": q("SELECT COUNT(*) FROM influencers")[0][0],
        "classified": dict(q("SELECT status, COUNT(*) FROM classifications GROUP BY status")),
        "shortlisted": q("SELECT COUNT(*) FROM enriched")[0][0],
        "messages": dict(q("SELECT review_status, COUNT(*) FROM messages GROUP BY review_status")),
        "outreach": [{"channel": c, "status": s, "count": n} for c, s, n in q("SELECT channel,status,COUNT(*) FROM outreach_log GROUP BY channel,status")],
        "send_mode": SETTINGS.send_mode,
        "llm_provider": SETTINGS.llm_provider,
    }


@app.get("/api/influencers")
def influencers(conn=Depends(get_conn)):
    rows = conn.execute("SELECT i.*, c.status AS filter_status, c.reasons, c.score FROM influencers i LEFT JOIN classifications c USING(influencer_id) ORDER BY i.influencer_id")
    return [dict(r) for r in rows]


@app.get("/api/shortlist")
def shortlist(conn=Depends(get_conn)):
    return [json.loads(r["record_json"]) for r in conn.execute("SELECT record_json FROM enriched ORDER BY influencer_id")]


@app.get("/api/messages")
def messages(conn=Depends(get_conn)):
    out = []
    for r in conn.execute("SELECT m.*, e.record_json FROM messages m JOIN enriched e USING(influencer_id) ORDER BY influencer_id"):
        d = dict(r)
        rec = json.loads(d.pop("record_json"))
        d["name"], d["contact_email"] = rec["name"], rec["contact_email"]
        out.append(d)
    return out


@app.post("/api/messages/{influencer_id}/review")
def review(influencer_id: str, body: ReviewBody, conn=Depends(get_conn)):
    try:
        n = sender.set_review(conn, body.status, [influencer_id])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not n:
        raise HTTPException(404, "message not found")
    return {"influencer_id": influencer_id, "review_status": body.status}


@app.post("/api/pipeline/run")
def run(body: RunBody, conn=Depends(get_conn)):
    logs: list[str] = []
    summary = run_pipeline(conn, SETTINGS, auto_approve=body.auto_approve, categories=body.categories,
                           force_messages=body.force_messages, progress=logs.append)
    return {"summary": summary, "log": logs}


@app.post("/api/send")
def send(body: SendBody, conn=Depends(get_conn)):
    try:
        res = sender.send_emails(conn, SETTINGS, limit=body.limit)
    except llm.ConfigError as exc:
        raise HTTPException(400, str(exc))
    res["dm_queued"] = sender.queue_dms(conn)
    exports.export_all(conn, SETTINGS.out_dir)
    return res


@app.get("/api/outreach")
def outreach(conn=Depends(get_conn)):
    return [dict(r) for r in conn.execute("SELECT * FROM outreach_log ORDER BY id DESC")]


@app.get("/api/tracker")
def tracker(conn=Depends(get_conn)):
    head = ["Influencer", "Email / Profile", "Channel", "Message Generated", "Sent", "Date", "Status", "Mode", "Detail"]
    return [dict(zip(head, r)) for r in exports.tracker_rows(conn)]


@app.get("/api/dm-queue")
def dm_queue(conn=Depends(get_conn)):
    sender.queue_dms(conn)
    return sender.dm_queue(conn)


@app.post("/api/dm/{influencer_id}/mark-sent")
def dm_mark(influencer_id: str, conn=Depends(get_conn)):
    try:
        sender.mark_dm_sent(conn, influencer_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"influencer_id": influencer_id, "status": "MANUAL_SENT"}
