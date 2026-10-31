"""Stage 4: Profile enrichment for shortlisted influencers.

Uses ONLY data already in the dataset (plus values derived from it). Anything that is not
in the dataset is written as 'Not Available' / 'Not Found' - never guessed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import urlparse

from .utils import NOT_AVAILABLE, NOT_FOUND, is_valid_email, now_iso

FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.in", "outlook.com", "hotmail.com", "icloud.com", "proton.me", "protonmail.com"}
CAUTION_NOTE_PATTERNS = ("not confirmed", "management-agency", "agency domain", "reader/editorial")


def handle_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return NOT_AVAILABLE
    h = path.split("/")[0]
    return h if h.startswith("@") else "@" + h


def classify_email(email: str, notes: str) -> tuple[str, str]:
    """Return (email_type, caution_reason). Caution = do not auto-send."""
    if not is_valid_email(email):
        return "none", ""
    domain = email.split("@")[1].lower()
    etype = "personal_free_mail" if domain in FREE_MAIL else "business_domain"
    n = (notes or "").lower()
    caution = next((p for p in CAUTION_NOTE_PATTERNS if p in n), "")
    return etype, (f"notes flag: '{caution}'" if caution else "")


def follower_tier(n: int) -> str:
    if n < 25_000:
        return "lower micro (5K-25K)"
    if n < 60_000:
        return "mid micro (25K-60K)"
    return "upper micro (60K-100K)"


def enrich(inf: dict) -> dict:
    email = inf["email"] if is_valid_email(inf["email"]) else NOT_FOUND
    etype, caution = classify_email(email, inf.get("notes", ""))
    country = inf["country"] if inf["country"] and inf["country"].lower() != "not publicly available" else NOT_AVAILABLE
    return {
        "influencer_id": inf["influencer_id"],
        "name": inf["name"],
        "handle": handle_from_url(inf["profile_url"]),
        "platform": inf["platform"],
        "profile_url": inf["profile_url"],
        "followers": inf["followers"],
        "follower_tier": follower_tier(inf["followers"]),
        "engagement_rate": inf["engagement_rate"],
        "niche": inf["niche"],
        "content_themes": [t.strip() for t in re.split(r"[;,]", inf["content_themes"]) if t.strip()],
        "contact_email": email,
        "email_type": etype,
        "email_caution": caution,
        "email_source": inf["email_source"] or NOT_AVAILABLE,
        "instagram_url": inf["instagram_url"] or NOT_AVAILABLE,
        "youtube_url": inf["youtube_url"] or NOT_AVAILABLE,
        "tiktok_url": inf["tiktok_url"] or NOT_AVAILABLE,
        "website": NOT_AVAILABLE,            # not in dataset
        "audience_age": NOT_AVAILABLE,       # not in dataset
        "audience_gender": NOT_AVAILABLE,    # not in dataset
        "audience_geography": country,       # creator country is the only geography in the dataset
        "metrics_source_url": inf["metrics_source_url"],
        "metrics_snapshot": inf["metrics_snapshot"],
        "last_verified": inf["last_verified"],
        "data_notes": inf["notes"] or "",
    }


def run(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT i.* FROM influencers i JOIN classifications c USING(influencer_id) "
        "WHERE c.status='PASSED' ORDER BY i.influencer_id"
    ).fetchall()
    ts = now_iso()
    with conn:
        conn.execute("DELETE FROM enriched")
        conn.executemany(
            "INSERT INTO enriched VALUES (?,?,?)",
            [(r["influencer_id"], json.dumps(enrich(dict(r)), ensure_ascii=False), ts) for r in rows],
        )
    with_email = sum(1 for r in rows if is_valid_email(r["email"]))
    return {"shortlisted": len(rows), "with_email": with_email, "email_not_found": len(rows) - with_email}
