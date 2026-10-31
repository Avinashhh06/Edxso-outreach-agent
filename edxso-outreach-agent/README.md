# EDXSO Automated Micro-Influencer Outreach System

Discovery → Data Collection → Filtering → Enrichment → AI Personalization → Review → Sending → Tracking

An agent-style pipeline (Python, SQLite, FastAPI) built strictly on `data/influencer_dataset_50.csv`.
It never scrapes, never invents emails, metrics or audience data. Missing values are written as `Not Found` / `Not Available`.

## 1. Technology stack
Python 3.10+ · SQLite (stdlib) · FastAPI + Uvicorn · Requests · PyYAML · python-dotenv · smtplib (Gmail SMTP) · unittest

## 2. APIs / tools used
| Purpose | Tool | Needed? |
|---|---|---|
| Personalization LLM | Gemini REST API **or** any OpenAI-compatible API (OpenAI, Groq, Together, Ollama) | Optional. `LLM_PROVIDER=none` uses the offline template engine |
| Email delivery | Gmail SMTP + App Password | Optional. Default is `dry_run` (simulated) |
| Instagram DMs | None. Manual queue (automation would break platform rules) | – |

## 3. Data sources
Only the supplied CSV (50 profiles; 46 Instagram, 4 YouTube; 9 niches). Per-row provenance is kept in the CSV columns
`Metrics Source URL`, `Metrics Snapshot`, `Email Source`, `Last Verified Date`, `Notes` (third-party directories such as Qoruz, Modash, HypeAuditor).
For YouTube rows, "followers" = subscribers.

## 4. Discovery methodology
The CSV is the discovery output: 50 profiles compiled from public creator directories and bios, with a source URL per row.
`discovery.py` validates required columns, rejects malformed or duplicate rows (by ID and profile URL), and loads them into SQLite.
Dataset facts: 23 profiles are within 5K–100K followers, 27 are above 100K (kept in the dataset, and failed by the filter with a clear reason).

## 5. Filtering logic (`filtering.py`, rules in `config.yaml`)
Every profile gets PASSED/FAILED plus a reason. Hard checks:
1. Platform allowed 2. Followers 5,000–100,000 3. Category in `active_categories` 4. Content relevance (themes match niche keywords)
5. Engagement ≥ `min_engagement_rate` 6. Audience quality (fake-follower % parsed from Notes, only if present)
7. Geography (never fails on unknown country unless `require_known_country: true`) 8. Brand fit (excludes editorial/non-individual accounts via Notes)
Email is an informational check: a profile without email can be shortlisted (DM only) but is never emailed.
Score (0–100) = 50·min(ER/5,1) + 30 if followers in range + 20 if valid email.

**Complete category implemented: Parenting** (default). Result: 6 Parenting profiles → 4 pass.
Shelly Narang fails (71.57% fake followers per Notes), Samhita RC fails (ER 0.05% < 0.2%).
Change `active_categories` or run `python main.py run --categories Fashion,Beauty` for any of the 9 niches.

## 6. Enrichment (`enrichment.py`)
Name, handle, platform, URL, followers + tier, ER, niche, content themes, contact email (+ type: personal/business, caution flag, source),
Instagram/YouTube/TikTok URLs, creator country. Website, audience age and audience gender are not in the dataset → `Not Available`.

## 7. AI model and prompt (`llm.py`, `personalization.py`)
No model is hard-coded: you set `LLM_MODEL`. The system prompt (see `SYSTEM_PROMPT`) restricts the model to the supplied facts,
forbids invented posts/numbers/"recent post" claims, requires an email body of 60–90 words, a DM of 15–30 words, and JSON-only output.

## 8. Personalization logic
Facts per creator (first name, platform, niche, content themes, company, collaboration angle, value proposition, CTA) → LLM → **validator**
(word counts, no placeholders, no digits not in facts, no unverifiable "recent/latest" claims, company + sender named, a niche/theme signal present)
→ up to 3 retries with the error list fed back → offline template fallback. Template mode varies wording per creator by a stable hash.
Greeting, signature (Avinash Sharma, Marketing Team, EDXSO, website) and opt-out line are added by code, not by the model.

## 9. Sending mechanism (`sender.py`)
- Nothing sends before **review approval** (CLI or dashboard).
- `SEND_MODE=dry_run` (default): logs `SIMULATED`, transmits nothing.
- `SEND_MODE=live` + `TEST_RECIPIENT`: every email is redirected to you (`TEST_SENT`). Use this first.
- `SEND_MODE=live` without `TEST_RECIPIENT`: real send via Gmail SMTP. Daily cap and delay from `config.yaml`.
- Duplicate prevention per influencer **and** per email address. Skips influencers with `Not Found` email. Skips emails flagged as agency/editorial/unconfirmed.
- Instagram DMs: manual queue (`dm_manual_queue.csv`, dashboard "DM queue"), then `python main.py dm --mark-sent INF_036`.
- Outreach log table `outreach_log` → `outputs/outreach_tracker.csv`.

## 10. Setup (VS Code)
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   |  source .venv/bin/activate  (Mac/Linux)
pip install -r requirements.txt
python main.py doctor           # shows what is configured / missing
python main.py setup            # optional: asks for LLM key, Gmail app password, test recipient -> writes .env
python main.py run              # full pipeline up to review
python main.py review           # read messages; then:
python main.py review --approve-all      # or --approve INF_036 INF_039
python main.py send             # dry-run by default
python main.py serve            # dashboard + API: http://127.0.0.1:8000  (docs at /docs)
python -m unittest discover -s tests -v
```
Gmail App Password: Google Account → Security → 2-Step Verification → App passwords. Put it in `SMTP_APP_PASSWORD`.

## 11. Outputs (`outputs/`)
`influencer_dataset_with_status.csv` (all 50 + PASSED/FAILED + reason) · `filter_report.csv` (per-check) · `enriched_shortlist.csv/json` ·
`personalized_messages.csv/.md` · `outreach_tracker.csv` · `dm_manual_queue.csv` · `outreach.db`.
The files shipped in this zip were produced by an offline run (template mode, dry-run send, auto-approved) only to demonstrate the format.

## 12. Limitations
- Only 2 of 4 shortlisted Parenting profiles have a public email; the other 2 are DM-only. Emails come from bios/third-party pages and are not verified deliverable.
- No "recent post" personalization: the dataset has content themes, not individual posts, so the system refuses to claim it saw specific content.
- Audience age/gender and website are not in the dataset.
- Engagement rates come from third-party snapshots and may differ from live values. Two Notes flag ambiguous attribution.
- No live scraping, no Instagram DM automation (by design).
- LLM calls and live SMTP require your credentials; the code paths are unit-tested with mocks, and a first real run is yours to verify (use `TEST_RECIPIENT`).

## 13. Scaling to 500+
Stages are idempotent and keyed by `influencer_id`; filtering is O(n) and config-driven; swap `discovery.py` for an API/directory ingester
with the same `store()` interface; SQLite → Postgres by replacing `db.py`; add a queue worker for LLM calls and sending.
