"""Command line interface:  python main.py <command>"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

from . import db, exports, llm, sender
from .config import ROOT, load_settings
from .pipeline import run_pipeline


def _conn(s):
    return db.connect(s.db_path)


# ---------------------------------------------------------------- doctor / setup
def cmd_doctor(s, _args) -> int:
    print("EDXSO outreach agent - configuration check\n")
    print(f"dataset      : {'OK' if s.data_csv.exists() else 'MISSING'}  {s.data_csv}")
    try:
        llm.check_config(s)
        print(f"LLM          : OK  provider={s.llm_provider}" + (f" model={s.llm_model}" if s.llm_provider != 'none' else " (offline template mode)"))
    except llm.ConfigError as exc:
        print(f"LLM          : NEEDS INPUT  {exc}")
    live = s.send_mode == "live"
    smtp_ok = all([s.smtp_user, s.smtp_password, s.from_email])
    print(f"send mode    : {s.send_mode}" + ("  (emails are only simulated)" if not live else ""))
    print(f"SMTP         : {'OK' if smtp_ok else 'not configured (needed only for live sending)'}")
    if live and s.test_recipient:
        print(f"TEST_RECIPIENT: {s.test_recipient}  -> all live emails are redirected here")
    elif live:
        print("WARNING      : live mode without TEST_RECIPIENT sends to REAL influencers")
    print(f"database     : {s.db_path}")
    print("\nTip: run `python main.py setup` to be asked for anything missing.")
    return 0


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else (" [keep current]" if default and secret else "")
    val = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
    return val or default


def cmd_setup(s, _args) -> int:
    print("Interactive setup. Press Enter to keep the current value. Nothing is sent anywhere; values are written to .env only.\n")
    env = {}
    print("-- LLM for personalization --")
    print("Options: gemini | openai_compatible (OpenAI/Groq/Ollama...) | none (offline templates, no key needed)")
    env["LLM_PROVIDER"] = _ask("LLM_PROVIDER", s.llm_provider)
    if env["LLM_PROVIDER"] != "none":
        env["LLM_MODEL"] = _ask("LLM_MODEL (exact model name from your provider's docs)", s.llm_model)
        env["LLM_API_KEY"] = _ask("LLM_API_KEY", s.llm_api_key, secret=True)
        if env["LLM_PROVIDER"] == "openai_compatible":
            env["LLM_BASE_URL"] = _ask("LLM_BASE_URL", s.llm_base_url)
    print("\n-- Email sending (Gmail SMTP + App Password; skip if you only want dry-run) --")
    env["SEND_MODE"] = _ask("SEND_MODE (dry_run|live)", s.send_mode)
    env["SMTP_USER"] = _ask("SMTP_USER (your Gmail address)", s.smtp_user)
    env["SMTP_APP_PASSWORD"] = _ask("SMTP_APP_PASSWORD (16-char Google App Password)", s.smtp_password, secret=True)
    env["FROM_EMAIL"] = _ask("FROM_EMAIL", s.from_email or env["SMTP_USER"])
    env["TEST_RECIPIENT"] = _ask("TEST_RECIPIENT (your own email for safe live tests)", s.test_recipient)
    env_path = ROOT / ".env"
    lines = [f"{k}={v}" for k, v in env.items() if v is not None]
    lines += [f"SMTP_HOST={s.smtp_host}", f"SMTP_PORT={s.smtp_port}", "DB_PATH=outputs/outreach.db"]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSaved {env_path}. Run `python main.py doctor` to verify.")
    return 0


# ---------------------------------------------------------------- pipeline commands
def cmd_run(s, a) -> int:
    conn = _conn(s)
    cats = [c.strip() for c in a.categories.split(",")] if a.categories else None
    summary = run_pipeline(conn, s, auto_approve=a.auto_approve, send=a.send, force_messages=a.force_messages, categories=cats)
    if "stopped_at" in summary:
        print(f"\nStopped at stage '{summary['stopped_at']}'. Fix the issue above and re-run (stages are idempotent).")
        return 1
    return 0


def cmd_review(s, a) -> int:
    conn = _conn(s)
    if a.approve_all:
        print(f"approved {sender.set_review(conn, 'approved')} messages")
    elif a.approve:
        print(f"approved {sender.set_review(conn, 'approved', a.approve)}")
    elif a.reject:
        print(f"rejected {sender.set_review(conn, 'rejected', a.reject)}")
    else:
        for r in conn.execute("SELECT m.*, e.record_json FROM messages m JOIN enriched e USING(influencer_id) ORDER BY influencer_id"):
            rec = json.loads(r["record_json"])
            print(f"\n=== {r['influencer_id']} | {rec['name']} | {rec['contact_email']} | review={r['review_status']} | {r['generator']}")
            print(f"Subject: {r['email_subject']}\n{r['email_body']}\n--- DM ---\n{r['dm_text']}\n[{r['validation']}]")
    exports.export_all(conn, s.out_dir)
    return 0


def cmd_send(s, a) -> int:
    conn = _conn(s)
    try:
        res = sender.send_emails(conn, s, limit=a.limit)
    except llm.ConfigError as exc:
        print(f"Cannot send: {exc}")
        return 1
    res["dm_queued"] = sender.queue_dms(conn)
    print(json.dumps(res, indent=2))
    exports.export_all(conn, s.out_dir)
    return 0


def cmd_dm(s, a) -> int:
    conn = _conn(s)
    if a.mark_sent:
        sender.mark_dm_sent(conn, a.mark_sent)
        print(f"{a.mark_sent} marked MANUAL_SENT")
    else:
        sender.queue_dms(conn)
        for d in sender.dm_queue(conn):
            print(f"\n[{d['status']}] {d['influencer_id']} {d['name']}\n{d['profile_url']}\n{d['dm_text']}")
    exports.export_all(conn, s.out_dir)
    return 0


def cmd_export(s, _a) -> int:
    for f in exports.export_all(_conn(s), s.out_dir):
        print(f)
    return 0


def cmd_status(s, _a) -> int:
    conn = _conn(s)
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    print("influencers :", q("SELECT COUNT(*) FROM influencers")[0][0])
    print("classified  :", {r[0]: r[1] for r in q("SELECT status, COUNT(*) FROM classifications GROUP BY status")})
    print("shortlisted :", q("SELECT COUNT(*) FROM enriched")[0][0])
    print("messages    :", {r[0]: r[1] for r in q("SELECT review_status, COUNT(*) FROM messages GROUP BY review_status")})
    print("outreach    :", {f"{r[0]}/{r[1]}": r[2] for r in q("SELECT channel,status,COUNT(*) FROM outreach_log GROUP BY channel,status")})
    return 0


def cmd_serve(s, a) -> int:
    import uvicorn
    uvicorn.run("outreach.api:app", host=a.host, port=a.port, reload=False)
    return 0


def cmd_reset(s, a) -> int:
    if not a.yes:
        print("This deletes the local database (outputs/outreach.db) including the outreach log. Re-run with --yes to confirm.")
        return 1
    Path(s.db_path).unlink(missing_ok=True)
    print("database deleted")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="main.py", description="EDXSO micro-influencer outreach agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check configuration")
    sub.add_parser("setup", help="interactive prompts for keys/credentials, writes .env")
    r = sub.add_parser("run", help="run the full pipeline")
    r.add_argument("--categories", help="comma list overriding config, e.g. Fashion,Beauty")
    r.add_argument("--auto-approve", action="store_true", help="skip manual review (not recommended)")
    r.add_argument("--send", action="store_true", help="also run the sending layer (respects SEND_MODE)")
    r.add_argument("--force-messages", action="store_true", help="regenerate messages that already exist")
    v = sub.add_parser("review", help="list / approve / reject messages")
    v.add_argument("--approve-all", action="store_true")
    v.add_argument("--approve", nargs="+", metavar="ID")
    v.add_argument("--reject", nargs="+", metavar="ID")
    sd = sub.add_parser("send", help="send (or simulate) approved emails")
    sd.add_argument("--limit", type=int)
    d = sub.add_parser("dm", help="manual Instagram DM queue")
    d.add_argument("--mark-sent", metavar="ID")
    sub.add_parser("export", help="re-generate output files")
    sub.add_parser("status", help="counts")
    sv = sub.add_parser("serve", help="start FastAPI + dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    rs = sub.add_parser("reset", help="delete local database")
    rs.add_argument("--yes", action="store_true")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = load_settings()
    handlers = {"doctor": cmd_doctor, "setup": cmd_setup, "run": cmd_run, "review": cmd_review, "send": cmd_send,
                "dm": cmd_dm, "export": cmd_export, "status": cmd_status, "serve": cmd_serve, "reset": cmd_reset}
    try:
        return handlers[args.cmd](s, args)
    except llm.ConfigError as exc:
        print(f"Configuration needed: {exc}\nRun `python main.py setup`.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
