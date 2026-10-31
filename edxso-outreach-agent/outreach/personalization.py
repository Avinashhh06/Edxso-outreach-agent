"""Stage 5: AI personalization (email pitch 60-90 words + Instagram DM 15-30 words).

Flow per influencer:
  facts (from enriched record only) -> LLM -> validator -> retry with feedback (max N)
  -> offline template fallback if LLM unavailable/invalid.
Every message is validated for length, grounding (no invented numbers / "recent post" claims),
placeholders and company mention. The validation result is stored with the message.
"""
from __future__ import annotations

import json
import re
import sqlite3
import zlib

from . import llm
from .config import Settings
from .utils import now_iso, word_count

NICHE_NOUN = {  # how each niche reads inside a sentence
    "Fintech": "finance", "Crypto": "crypto", "Fitness": "fitness", "Beauty": "beauty",
    "Fashion": "fashion", "Parenting": "parenting", "Gaming": "gaming",
    "Technology": "tech", "Lifestyle": "lifestyle",
}
BANNED = ["recent", "latest", "yesterday", "last week", "last month", "just posted", "your video on", "your post on"]
STRIP_SUFFIX = re.compile(r"\s+(influencer|blogger|creator|content|expert)$", re.I)

SYSTEM_PROMPT = """You write short, natural outreach messages for {company}, a brand contacting micro-influencers.
HARD RULES:
- Use ONLY the facts in the JSON the user sends. Never invent posts, videos, numbers, awards, brands, results, or the creator's opinions.
- Do NOT claim to have seen any specific or recent post. You may only refer to the creator's general content themes and niche.
- No placeholders, no digits, no hashtags. No emojis in the email (max one in the DM).
- Sound human and specific to THIS creator; do not reuse stock phrasing. Warm, concise, professional.
- email_body: {ew0}-{ew1} words, EXCLUDING greeting and signature (they are added by code). Mention the niche or themes, the audience fit, the proposed collaboration, the value proposition, and end by asking them to share preferences/rates/availability.
- dm: {dw0}-{dw1} words, casual, mention the sender first name and {company}, reference the creator's themes, end with a light question.
- email_subject: under 70 characters, specific, no clickbait.
Return ONLY JSON: {{"email_subject": "...", "email_body": "...", "dm": "..."}}"""


# ---------------------------------------------------------------- helpers
def greeting_name(name: str, niche: str) -> str:
    base = re.split(r"[|(@]", name)[0].strip() or name.strip()
    toks = base.split()
    stop = {w.lower() for w in [niche, "gaming", "tech", "finance", "crypto", "wedding", "official"]}
    if len(toks) > 3:                       # long handle-like name: greet by first word
        return toks[0]
    if any(t.lower() in stop for t in toks) or len(toks) == 1:
        return base
    return toks[0]


def theme_phrase(themes: list[str], limit: int = 2, niche: str = "", max_words: int = 4) -> str:
    cleaned: list[str] = []
    for t in themes:
        t = STRIP_SUFFIX.sub("", t.strip())
        if len(t) < 5 or t.lower() in {c.lower() for c in cleaned}:
            continue
        if niche and t.lower() == niche.lower() and len(themes) > 1:
            continue                      # avoid "parenting content in the parenting space"
        cleaned.append(t)
    if not cleaned:
        return ""
    phrase = cleaned[0] if any(" and " in t for t in cleaned) else " and ".join(cleaned[:limit])
    if word_count(phrase) > max_words:       # keep DM within its word limit and the sentence readable
        phrase = cleaned[0]
    return phrase if word_count(phrase) <= max_words else ""


def build_facts(rec: dict, settings: Settings) -> dict:
    c, camp = settings.section("company"), settings.section("campaign")
    return {
        "creator_first_name": greeting_name(rec["name"], rec["niche"]),
        "creator_display_name": rec["name"],
        "platform": rec["platform"],
        "niche": NICHE_NOUN.get(rec["niche"], rec["niche"].lower()),
        "content_themes": rec["content_themes"],
        "company": c["name"],
        "sender_first_name": c["sender_first_name"],
        "collaboration": camp["collab_angle"],
        "value_proposition": camp["value_proposition"],
        "call_to_action": camp["call_to_action"],
    }


def assemble_email(first: str, body: str, settings: Settings) -> str:
    c, camp = settings.section("company"), settings.section("campaign")
    sig = f"Best regards,\n{c['sender_name']}\n{c['sender_title']}\n{c['name']}\n{c['website']}"
    opt = f"\n\n{camp['optout_line']}" if camp.get("optout_line") else ""
    return f"Hi {first},\n\n{body.strip()}\n\n{sig}{opt}"


# ---------------------------------------------------------------- validation
def validate(out: dict, facts: dict, settings: Settings) -> list[str]:
    p = settings.section("personalization")
    (e0, e1), (d0, d1) = p["email_words"], p["dm_words"]
    errs: list[str] = []
    for k in ("email_subject", "email_body", "dm"):
        if not isinstance(out.get(k), str) or not out[k].strip():
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    body, dm, subj = out["email_body"], out["dm"], out["email_subject"]
    ew, dw = word_count(body), word_count(dm)
    if not e0 <= ew <= e1:
        errs.append(f"email_body has {ew} words, need {e0}-{e1}")
    if not d0 <= dw <= d1:
        errs.append(f"dm has {dw} words, need {d0}-{d1}")
    if len(subj) > 70:
        errs.append("subject longer than 70 chars")
    blob = f"{subj} {body} {dm}"
    low = blob.lower()
    if re.search(r"[\[\]{}<>]", blob):
        errs.append("contains placeholder-like brackets")
    allowed_digits = set(re.findall(r"\d+", json.dumps(facts)))
    bad_digits = [d for d in re.findall(r"\d+", blob) if d not in allowed_digits]
    if bad_digits:
        errs.append(f"contains numbers not in facts: {bad_digits}")
    hit = [b for b in BANNED if b in low]
    if hit:
        errs.append(f"unverifiable claim wording: {hit}")
    if facts["company"].lower() not in body.lower():
        errs.append("company name missing in email body")
    if facts["company"].lower() not in dm.lower():
        errs.append("company name missing in dm")
    if facts["sender_first_name"].lower() not in dm.lower():
        errs.append("dm must mention sender first name")
    sig_tokens = {t.lower() for th in facts["content_themes"] for t in re.findall(r"[A-Za-z]{4,}", th)} | {facts["niche"].lower()}
    if not any(t in low for t in sig_tokens):
        errs.append("no personalization signal (niche/theme) found")
    if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", body):
        errs.append("emoji in email body")
    return errs


# ---------------------------------------------------------------- offline template engine
def _pick(options: list[str], seed: int, salt: int) -> str:
    return options[(seed + salt * 7) % len(options)]


def template_message(facts: dict) -> dict:
    """Deterministic, facts-only fallback. Varies by creator via a stable hash seed."""
    seed = zlib.crc32(facts["creator_display_name"].encode())
    f = facts
    themes = theme_phrase(f["content_themes"], niche=f["niche"]) or f["niche"]
    plat, niche, co = f["platform"], f["niche"], f["company"]
    opener = _pick([
        f"I came across your {plat} content on {themes} and really liked how it speaks to your {niche} community.",
        f"Your {plat} work around {themes} stood out to us, especially for the {niche}-focused audience you have built.",
        f"We have been following creators in the {niche} space, and your {plat} content on {themes} caught our attention.",
    ], seed, 1)
    pitch = _pick([
        f"At {co} we are looking for creators like you for {f['collaboration']}, and we think your audience would be a natural fit.",
        f"{co} is planning {f['collaboration']} with a small group of {niche} creators, and your community looks like a strong match.",
        f"We would love to explore {f['collaboration']} with you at {co}, since your audience matches who we want to reach.",
    ], seed, 2)
    value = _pick([
        f"We offer {f['value_proposition']}.",
        f"Our goal is {f['value_proposition']}.",
        f"What we bring is {f['value_proposition']}.",
    ], seed, 3)
    cta = _pick([
        f"If you are open to it, please {f['call_to_action']}.",
        f"Would you be open to a chat? Please {f['call_to_action']}.",
        f"If this sounds interesting, kindly {f['call_to_action']}.",
    ], seed, 4)
    dm = _pick([
        f"Hey {f['creator_first_name']}! Loved your {themes} content. I'm {f['sender_first_name']} from {co}. Your {niche} audience looks like a great fit for a collab. Interested?",
        f"Hi {f['creator_first_name']}, really like your {themes} content! I'm {f['sender_first_name']} at {co}, and your {niche} community seems perfect for a collaboration. Open to chatting?",
        f"Hey {f['creator_first_name']}! Your {themes} content is great. {f['sender_first_name']} here from {co}. We think your {niche} audience fits a collab we're planning. Interested?",
    ], seed, 5)
    subject = _pick([
        f"Collaboration idea for your {niche} content",
        f"{co} x {f['creator_first_name']}: collaboration opportunity" if len(f["creator_first_name"]) <= 25 else f"Collaboration opportunity with {co}",
        f"Partnering on {niche} content with {co}",
    ], seed, 6)
    return {"email_subject": subject, "email_body": " ".join([opener, pitch, value, cta]), "dm": dm}


# ---------------------------------------------------------------- generation
def generate(rec: dict, settings: Settings) -> dict:
    facts = build_facts(rec, settings)
    p = settings.section("personalization")
    if settings.llm_provider != "none":
        system = SYSTEM_PROMPT.format(company=facts["company"], ew0=p["email_words"][0], ew1=p["email_words"][1],
                                      dw0=p["dm_words"][0], dw1=p["dm_words"][1])
        user = "FACTS:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
        last_errs: list[str] = []
        for attempt in range(1, p["max_retries"] + 1):
            prompt = user if not last_errs else user + "\n\nYour previous attempt failed validation: " + "; ".join(last_errs) + ". Fix these and return JSON only."
            try:
                out = llm.complete_json(settings, system, prompt)
            except llm.LLMError as exc:
                last_errs = [str(exc)]
                break
            last_errs = validate(out, facts, settings)
            if not last_errs:
                return _pack(out, facts, settings, f"llm:{settings.llm_provider}/{settings.llm_model}", f"attempt {attempt}")
        out = template_message(facts)
        note = "LLM output rejected, used template fallback: " + "; ".join(last_errs)
        gen = "template-fallback"
    else:
        out, note, gen = template_message(facts), "offline template mode (LLM_PROVIDER=none)", "template"
    errs = validate(out, facts, settings)
    if errs:  # should never happen; surfaced loudly rather than hidden
        note += " | TEMPLATE VALIDATION FAILED: " + "; ".join(errs)
    return _pack(out, facts, settings, gen, note)


def _pack(out: dict, facts: dict, settings: Settings, generator: str, note: str) -> dict:
    return {
        "email_subject": out["email_subject"].strip(),
        "email_text": assemble_email(facts["creator_first_name"], out["email_body"], settings),
        "dm_text": out["dm"].strip(),
        "generator": generator,
        "validation": f"email_words={word_count(out['email_body'])}; dm_words={word_count(out['dm'])}; {note}",
    }


def run(conn: sqlite3.Connection, settings: Settings, force: bool = False) -> dict:
    llm.check_config(settings)
    recs = [json.loads(r["record_json"]) for r in conn.execute("SELECT record_json FROM enriched ORDER BY influencer_id")]
    done = {r[0] for r in conn.execute("SELECT influencer_id FROM messages")}
    made = skipped = 0
    for rec in recs:
        if rec["influencer_id"] in done and not force:
            skipped += 1
            continue
        m = generate(rec, settings)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages (influencer_id,email_subject,email_body,dm_text,generator,validation,review_status,generated_at,reviewed_at) "
                "VALUES (?,?,?,?,?,?,'pending',?,NULL)",
                (rec["influencer_id"], m["email_subject"], m["email_text"], m["dm_text"], m["generator"], m["validation"], now_iso()),
            )
        made += 1
    return {"generated": made, "skipped_existing": skipped}
