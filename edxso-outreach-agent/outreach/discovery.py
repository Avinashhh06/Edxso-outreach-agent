"""Stage 1+2: Discovery / data collection.

The 50-profile dataset was compiled from public third-party directories (see the
'Metrics Source URL' column per row). This module loads it, validates the schema,
removes duplicates and stores it. NOTHING is scraped or invented here.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path

from .utils import NOT_FOUND, clean

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = [
    "Influencer ID", "Influencer Name", "Platform", "Profile URL", "Follower Count",
    "Engagement Rate (%)", "Category / Niche", "Content Themes", "Contact Email",
]

COLUMN_MAP = {
    "Influencer ID": "influencer_id", "Influencer Name": "name", "Platform": "platform",
    "Profile URL": "profile_url", "Follower Count": "followers",
    "Engagement Rate (%)": "engagement_rate", "Category / Niche": "niche",
    "Content Themes": "content_themes", "Contact Email": "email",
    "Instagram URL": "instagram_url", "YouTube URL": "youtube_url", "TikTok URL": "tiktok_url",
    "Country": "country", "Metrics Source URL": "metrics_source_url",
    "Metrics Snapshot": "metrics_snapshot", "Email Source": "email_source",
    "Last Verified Date": "last_verified", "Notes": "notes",
}


class DatasetError(RuntimeError):
    pass


def load_csv(path: Path) -> tuple[list[dict], list[str]]:
    """Return (valid_rows, problems). Bad rows are reported, never silently fixed."""
    if not Path(path).exists():
        raise DatasetError(f"Dataset not found: {path}")
    problems: list[str] = []
    rows: list[dict] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise DatasetError(f"CSV missing required columns: {missing}")
        for line_no, raw in enumerate(reader, start=2):
            row = {COLUMN_MAP[k]: clean(raw.get(k)) for k in COLUMN_MAP}
            if not row["influencer_id"] or not row["name"]:
                problems.append(f"line {line_no}: missing id/name, skipped")
                continue
            try:
                row["followers"] = int(float(row["followers"]))
                row["engagement_rate"] = float(row["engagement_rate"])
            except ValueError:
                problems.append(f"line {line_no} ({row['influencer_id']}): bad followers/engagement, skipped")
                continue
            if row["influencer_id"] in seen_ids or row["profile_url"].lower() in seen_urls:
                problems.append(f"line {line_no} ({row['influencer_id']}): duplicate, skipped")
                continue
            seen_ids.add(row["influencer_id"])
            seen_urls.add(row["profile_url"].lower())
            row["email"] = row["email"] or NOT_FOUND
            rows.append(row)
    return rows, problems


def store(conn: sqlite3.Connection, rows: list[dict]) -> int:
    cols = list(COLUMN_MAP.values())
    sql = (f"INSERT OR REPLACE INTO influencers ({','.join(cols)}) "
           f"VALUES ({','.join('?' for _ in cols)})")
    with conn:
        conn.executemany(sql, [[r[c] for c in cols] for r in rows])
    return len(rows)


def run(conn: sqlite3.Connection, csv_path: Path) -> dict:
    rows, problems = load_csv(csv_path)
    for p in problems:
        log.warning(p)
    n = store(conn, rows)
    return {"loaded": n, "problems": problems}
