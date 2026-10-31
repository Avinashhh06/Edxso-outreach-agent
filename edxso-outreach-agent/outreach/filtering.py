"""Stage 3: Filtering & classification.

Every influencer is evaluated against explicit, config-driven checks. The output
for each profile is PASSED/FAILED plus a plain-English reason, so nothing is a black box.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, asdict

from .utils import NOT_FOUND, is_valid_email, now_iso

_FAKE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*fake followers", re.I)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    severity: str = "hard"   # hard = failing blocks shortlist; info = reported only


@dataclass
class Result:
    influencer_id: str
    status: str              # PASSED | FAILED
    reasons: str
    score: float
    checks: list[Check]


def parse_fake_pct(notes: str) -> float | None:
    m = _FAKE_RE.search(notes or "")
    return float(m.group(1)) if m else None


def evaluate(inf: dict, cfg: dict) -> Result:
    f = cfg["filters"]
    kw_map = {k.lower(): v for k, v in cfg.get("category_keywords", {}).items()}
    cats = [c.lower() for c in f["active_categories"]]
    checks: list[Check] = []

    # 1. platform
    ok = inf["platform"] in f["allowed_platforms"]
    checks.append(Check("platform", ok, f"{inf['platform']} " + ("allowed" if ok else f"not in {f['allowed_platforms']}")))

    # 2. follower range (micro-influencer definition)
    fol = inf["followers"]
    ok = f["followers_min"] <= fol <= f["followers_max"]
    checks.append(Check("followers", ok, f"{fol:,} " + ("within" if ok else "outside") + f" {f['followers_min']:,}-{f['followers_max']:,}"))

    # 3. category
    ok = inf["niche"].lower() in cats
    checks.append(Check("category", ok, f"niche '{inf['niche']}' " + ("is" if ok else "is not") + f" in active categories {f['active_categories']}"))

    # 4. content relevance: themes must contain a keyword of the influencer's own niche
    kws = kw_map.get(inf["niche"].lower(), [])
    themes = inf["content_themes"].lower()
    hits = [k for k in kws if k in themes]
    ok = bool(hits) if kws else True
    checks.append(Check("content_relevance", ok, f"themes match {hits}" if ok else f"themes '{inf['content_themes']}' match none of {kws}"))

    # 5. engagement
    er = inf["engagement_rate"]
    ok = er >= f["min_engagement_rate"]
    checks.append(Check("engagement", ok, f"ER {er}% " + (">=" if ok else "<") + f" min {f['min_engagement_rate']}%"))

    # 6. audience quality (only if data exists)
    fake = parse_fake_pct(inf.get("notes", ""))
    if fake is None:
        checks.append(Check("audience_quality", True, "no fake-follower data in sources (not assumed)", "info"))
    else:
        ok = fake <= f["max_fake_follower_pct"]
        checks.append(Check("audience_quality", ok, f"{fake}% fake followers " + ("<=" if ok else ">") + f" max {f['max_fake_follower_pct']}%"))

    # 7. geography
    country = inf["country"]
    known = country and country.lower() != "not publicly available"
    allowed = f.get("allowed_countries") or []
    if not known:
        ok = not f.get("require_known_country", False)
        checks.append(Check("geography", ok, "country not publicly available" + ("" if ok else " (required)")))
    elif allowed:
        ok = country in allowed
        checks.append(Check("geography", ok, f"{country} " + ("in" if ok else "not in") + f" {allowed}"))
    else:
        checks.append(Check("geography", True, f"{country}; no country restriction", "info"))

    # 8. brand fit (non-individual / editorial accounts)
    bad = [p for p in f.get("exclude_note_patterns", []) if p.lower() in (inf.get("notes") or "").lower()]
    checks.append(Check("brand_fit", not bad, "no exclusion flags in notes" if not bad else f"flagged: {bad}"))

    # info: email availability (needed for sending, not for shortlisting)
    has_email = is_valid_email(inf["email"])
    checks.append(Check("contact_email", True, "valid email on file" if has_email else "Not Found (sendable via manual DM only)", "info"))

    failed = [c for c in checks if c.severity == "hard" and not c.passed]
    status = "FAILED" if failed else "PASSED"
    reasons = "; ".join(f"{c.name}: {c.detail}" for c in failed) if failed else "all hard checks passed"
    score = round(50 * min(er / 5.0, 1.0) + (30 if checks[1].passed else 0) + (20 if has_email else 0), 1)
    return Result(inf["influencer_id"], status, reasons, score, checks)


def run(conn: sqlite3.Connection, cfg: dict) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM influencers ORDER BY influencer_id")]
    if not rows:
        raise RuntimeError("No influencers in DB. Run the discovery stage first.")
    ts = now_iso()
    results = [evaluate(r, cfg) for r in rows]
    with conn:
        conn.execute("DELETE FROM classifications")
        conn.executemany(
            "INSERT INTO classifications VALUES (?,?,?,?,?,?)",
            [(r.influencer_id, r.status, r.reasons, json.dumps([asdict(c) for c in r.checks]), r.score, ts) for r in results],
        )
    passed = sum(r.status == "PASSED" for r in results)
    return {"evaluated": len(results), "passed": passed, "failed": len(results) - passed}
