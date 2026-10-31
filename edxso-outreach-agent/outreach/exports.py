"""Deliverables: dataset+status, filter report, enriched shortlist, messages, outreach tracker."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from .utils import NOT_FOUND


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def tracker_rows(conn: sqlite3.Connection) -> list[list]:
    rows: list[list] = []
    for r in conn.execute("SELECT m.influencer_id, m.review_status, e.record_json FROM messages m JOIN enriched e USING(influencer_id) ORDER BY m.influencer_id"):
        rec = json.loads(r["record_json"])
        for channel in ("email", "instagram_dm"):
            last = conn.execute("SELECT * FROM outreach_log WHERE influencer_id=? AND channel=? ORDER BY id DESC LIMIT 1", (r["influencer_id"], channel)).fetchone()
            done = conn.execute(
                "SELECT created_at FROM outreach_log WHERE influencer_id=? AND channel=? AND status IN ('SENT','TEST_SENT','MANUAL_SENT') ORDER BY id DESC LIMIT 1",
                (r["influencer_id"], channel)).fetchone()
            status = last["status"] if last else {"pending": "AWAITING_REVIEW", "approved": "APPROVED_NOT_SENT", "rejected": "REJECTED"}[r["review_status"]]
            rows.append([
                rec["name"], rec["contact_email"] if channel == "email" else rec["profile_url"], channel, "Yes", "Yes" if done else "No",
                (done["created_at"] if done else (last["created_at"] if last else "")), status, last["mode"] if last else "", (last["detail"] if last else ""),
            ])
    return rows


def export_all(conn: sqlite3.Connection, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    # B. dataset with status (all discovered)
    p = out_dir / "influencer_dataset_with_status.csv"
    rows = [[r["name"], r["platform"], r["followers"], r["engagement_rate"], r["niche"], r["email"], r["profile_url"], r["content_themes"],
             r["status"], r["reasons"], r["score"]]
            for r in conn.execute("SELECT i.*, c.status, c.reasons, c.score FROM influencers i LEFT JOIN classifications c USING(influencer_id) ORDER BY i.influencer_id")]
    _write(p, ["Name", "Platform", "Followers", "Engagement (%)", "Niche", "Email", "Profile URL", "Content Theme", "Status", "Pass/Fail Reason", "Score"], rows)
    files.append(p)

    # filter report: one column per check
    p = out_dir / "filter_report.csv"
    names = ["platform", "followers", "category", "content_relevance", "engagement", "audience_quality", "geography", "brand_fit", "contact_email"]
    rows = []
    for r in conn.execute("SELECT i.influencer_id, i.name, c.status, c.checks_json FROM influencers i JOIN classifications c USING(influencer_id) ORDER BY i.influencer_id"):
        by = {c["name"]: c for c in json.loads(r["checks_json"])}
        rows.append([r["influencer_id"], r["name"], r["status"]] + [("PASS: " if by[n]["passed"] else "FAIL: ") + by[n]["detail"] for n in names])
    _write(p, ["ID", "Name", "Status"] + names, rows)
    files.append(p)

    # enriched shortlist
    recs = [json.loads(r["record_json"]) for r in conn.execute("SELECT record_json FROM enriched ORDER BY influencer_id")]
    (out_dir / "enriched_shortlist.json").write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")
    files.append(out_dir / "enriched_shortlist.json")
    p = out_dir / "enriched_shortlist.csv"
    cols = ["name", "platform", "profile_url", "followers", "engagement_rate", "niche", "content_themes", "contact_email", "email_type", "email_source",
            "instagram_url", "youtube_url", "tiktok_url", "website", "audience_age", "audience_gender", "audience_geography", "metrics_source_url"]
    _write(p, [c.replace("_", " ").title() for c in cols], [["; ".join(r[c]) if isinstance(r[c], list) else r[c] for c in cols] for r in recs])
    files.append(p)

    # C. messages
    msgs = conn.execute("SELECT m.*, e.record_json FROM messages m JOIN enriched e USING(influencer_id) ORDER BY m.influencer_id").fetchall()
    p = out_dir / "personalized_messages.csv"
    _write(p, ["Influencer", "Email To", "Email Subject", "Email Body", "Instagram DM", "Generator", "Validation", "Review Status"],
           [[json.loads(m["record_json"])["name"], json.loads(m["record_json"])["contact_email"], m["email_subject"], m["email_body"], m["dm_text"], m["generator"], m["validation"], m["review_status"]] for m in msgs])
    files.append(p)
    md = ["# Personalized outreach messages\n"]
    for m in msgs:
        rec = json.loads(m["record_json"])
        md += [f"## {rec['name']} ({rec['platform']}, {rec['followers']:,} followers, ER {rec['engagement_rate']}%)",
               f"- Email: {rec['contact_email']}", f"- Generator: {m['generator']} | {m['validation']}", f"- Review: {m['review_status']}", "",
               f"**Subject:** {m['email_subject']}", "", "```text", m["email_body"], "```", "", "**Instagram DM:**", "", "```text", m["dm_text"], "```", ""]
    (out_dir / "personalized_messages.md").write_text("\n".join(md), encoding="utf-8")
    files.append(out_dir / "personalized_messages.md")

    # D. tracker
    p = out_dir / "outreach_tracker.csv"
    _write(p, ["Influencer", "Email / Profile", "Channel", "Message Generated", "Sent", "Date", "Status", "Mode", "Detail"], tracker_rows(conn))
    files.append(p)

    # manual DM queue
    from .sender import dm_queue
    p = out_dir / "dm_manual_queue.csv"
    _write(p, ["Influencer ID", "Name", "Profile URL", "DM Text", "Status"], [[d["influencer_id"], d["name"], d["profile_url"], d["dm_text"], d["status"]] for d in dm_queue(conn)])
    files.append(p)
    return files
