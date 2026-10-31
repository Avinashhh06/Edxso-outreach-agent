"""SQLite storage. Plain sqlite3 -> zero extra dependencies."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS influencers (
    influencer_id TEXT PRIMARY KEY,
    name TEXT, platform TEXT, profile_url TEXT, followers INTEGER,
    engagement_rate REAL, niche TEXT, content_themes TEXT, email TEXT,
    instagram_url TEXT, youtube_url TEXT, tiktok_url TEXT, country TEXT,
    metrics_source_url TEXT, metrics_snapshot TEXT, email_source TEXT,
    last_verified TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
    influencer_id TEXT PRIMARY KEY,
    status TEXT,              -- PASSED | FAILED | OUT_OF_SCOPE
    reasons TEXT,             -- human readable
    checks_json TEXT,
    score REAL,
    run_at TEXT
);
CREATE TABLE IF NOT EXISTS enriched (
    influencer_id TEXT PRIMARY KEY,
    record_json TEXT,
    enriched_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    influencer_id TEXT PRIMARY KEY,
    email_subject TEXT, email_body TEXT, dm_text TEXT,
    generator TEXT, validation TEXT,
    review_status TEXT DEFAULT 'pending',   -- pending | approved | rejected
    generated_at TEXT, reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS outreach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id TEXT, channel TEXT,        -- email | instagram_dm
    recipient TEXT, status TEXT, mode TEXT,
    detail TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_inf ON outreach_log(influencer_id, channel);
CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT, status TEXT, detail TEXT, started_at TEXT, finished_at TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
