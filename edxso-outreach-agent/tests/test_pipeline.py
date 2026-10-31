import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from outreach import db, discovery, enrichment, filtering, personalization, sender
from outreach.config import load_settings
from outreach.pipeline import run_pipeline
from outreach.utils import NOT_FOUND, word_count


def fresh():
    s = load_settings()
    tmp = Path(tempfile.mkdtemp())
    s.db_path, s.out_dir = tmp / "t.db", tmp / "out"
    return s, db.connect(s.db_path)


class FilteringTests(unittest.TestCase):
    def setUp(self):
        self.s, self.conn = fresh()
        discovery.run(self.conn, self.s.data_csv)

    def test_loads_50_unique(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM influencers").fetchone()[0], 50)

    def test_every_row_has_pass_fail_and_reason(self):
        filtering.run(self.conn, self.s.cfg)
        rows = self.conn.execute("SELECT status, reasons FROM classifications").fetchall()
        self.assertEqual(len(rows), 50)
        self.assertTrue(all(r["status"] in {"PASSED", "FAILED"} and r["reasons"] for r in rows))

    def test_follower_range_enforced(self):
        filtering.run(self.conn, self.s.cfg)
        bad = self.conn.execute(
            "SELECT 1 FROM classifications c JOIN influencers i USING(influencer_id) WHERE c.status='PASSED' AND (i.followers<5000 OR i.followers>100000)"
        ).fetchone()
        self.assertIsNone(bad)

    def test_fake_follower_flag_fails(self):
        row = dict(self.conn.execute("SELECT * FROM influencers WHERE notes LIKE '%71.57%'").fetchone())
        r = filtering.evaluate(row, self.s.cfg)
        self.assertEqual(r.status, "FAILED")
        self.assertIn("audience_quality", r.reasons)

    def test_missing_email_does_not_fail_filter(self):
        cfg = copy.deepcopy(self.s.cfg)
        row = dict(self.conn.execute("SELECT * FROM influencers WHERE influencer_id='INF_037'").fetchone())
        self.assertEqual(row["email"], NOT_FOUND)
        self.assertEqual(filtering.evaluate(row, cfg).status, "PASSED")

    def test_category_override(self):
        cfg = copy.deepcopy(self.s.cfg)
        cfg["filters"]["active_categories"] = ["Beauty"]
        filtering.run(self.conn, cfg)
        niches = {r[0] for r in self.conn.execute("SELECT i.niche FROM influencers i JOIN classifications c USING(influencer_id) WHERE c.status='PASSED'")}
        self.assertEqual(niches, {"Beauty"})


class EnrichmentTests(unittest.TestCase):
    def test_never_invents_email_or_audience(self):
        s, conn = fresh()
        discovery.run(conn, s.data_csv)
        filtering.run(conn, s.cfg)
        enrichment.run(conn)
        for r in conn.execute("SELECT record_json FROM enriched"):
            rec = json.loads(r[0])
            src = conn.execute("SELECT email FROM influencers WHERE influencer_id=?", (rec["influencer_id"],)).fetchone()[0]
            self.assertEqual(rec["contact_email"], src if "@" in src else NOT_FOUND)
            self.assertEqual(rec["audience_age"], "Not Available")
            self.assertEqual(rec["website"], "Not Available")


class PersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.s, self.conn = fresh()
        discovery.run(self.conn, self.s.data_csv)

    def test_template_valid_for_all_50_rows_and_many_seeds(self):
        for r in self.conn.execute("SELECT * FROM influencers"):
            rec = enrichment.enrich(dict(r))
            for salt in range(12):  # exercise different template-variant seeds
                rec2 = dict(rec, name=f"{rec['name']}{'x' * salt}")
                facts = personalization.build_facts(rec2, self.s)
                out = personalization.template_message(facts)
                self.assertEqual(personalization.validate(out, facts, self.s), [], (rec["name"], salt, out))

    def test_validator_rejects_bad_output(self):
        rec = enrichment.enrich(dict(self.conn.execute("SELECT * FROM influencers WHERE influencer_id='INF_036'").fetchone()))
        facts = personalization.build_facts(rec, self.s)
        good = personalization.template_message(facts)
        self.assertEqual(personalization.validate(good, facts, self.s), [])
        for bad in (
            dict(good, email_body="too short"),
            dict(good, dm="Hey! EDXSO Avinash"),
            dict(good, email_body=good["email_body"] + " We saw your recent reel with 500000 views."),
            dict(good, email_body=good["email_body"].replace("EDXSO", "ACME")),
            dict(good, email_body="[Creator Name] " + good["email_body"]),
        ):
            self.assertTrue(personalization.validate(bad, facts, self.s))

    def test_llm_failure_falls_back_to_template(self):
        s = self.s
        s.llm_provider, s.llm_api_key, s.llm_model = "gemini", "k", "m"
        rec = enrichment.enrich(dict(self.conn.execute("SELECT * FROM influencers WHERE influencer_id='INF_036'").fetchone()))
        with mock.patch("outreach.llm.complete_json", side_effect=personalization.llm.LLMError("boom")):
            m = personalization.generate(rec, s)
        self.assertEqual(m["generator"], "template-fallback")

    def test_llm_good_output_accepted(self):
        s = self.s
        s.llm_provider, s.llm_api_key, s.llm_model = "gemini", "k", "m"
        rec = enrichment.enrich(dict(self.conn.execute("SELECT * FROM influencers WHERE influencer_id='INF_036'").fetchone()))
        facts = personalization.build_facts(rec, s)
        fake = personalization.template_message(facts)
        with mock.patch("outreach.llm.complete_json", return_value=fake):
            m = personalization.generate(rec, s)
        self.assertTrue(m["generator"].startswith("llm:"))
        self.assertTrue(60 <= word_count(fake["email_body"]) <= 90)


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.s, self.conn = fresh()
        run_pipeline(self.conn, self.s, auto_approve=True, progress=lambda _m: None)

    def test_pending_messages_not_sent(self):
        s, conn = fresh()
        run_pipeline(conn, s, auto_approve=False, progress=lambda _m: None)
        res = sender.send_emails(conn, s)
        self.assertEqual(res["simulated"], 0)

    def test_dry_run_simulates_only_valid_emails_and_dedupes(self):
        res = sender.send_emails(self.conn, self.s, sleep=lambda _x: None)
        self.assertEqual(res["mode"], "dry_run")
        self.assertEqual(res["simulated"], 2)
        self.assertEqual(res["skipped_no_email"], 2)
        again = sender.send_emails(self.conn, self.s, sleep=lambda _x: None)
        self.assertEqual(again["simulated"], 0)
        self.assertEqual(again["skipped_duplicate"], 2)
        n = self.conn.execute("SELECT COUNT(*) FROM outreach_log WHERE status='SKIPPED_NO_EMAIL'").fetchone()[0]
        self.assertEqual(n, 2)  # logged once, not on every run

    def test_live_without_smtp_creds_raises(self):
        self.s.send_mode = "live"
        with self.assertRaises(sender.ConfigError):
            sender.send_emails(self.conn, self.s, sleep=lambda _x: None)

    def test_live_test_redirects_to_test_recipient(self):
        self.s.send_mode, self.s.test_recipient = "live", "me@example.com"
        with mock.patch("outreach.sender._smtp_send") as m:
            res = sender.send_emails(self.conn, self.s, sleep=lambda _x: None)
        self.assertEqual(res["sent"], 2)
        self.assertTrue(all(c.args[1] == "me@example.com" for c in m.call_args_list))

    def test_live_real_send_blocks_duplicates(self):
        self.s.send_mode, self.s.test_recipient = "live", ""
        with mock.patch("outreach.sender._smtp_send") as m:
            sender.send_emails(self.conn, self.s, sleep=lambda _x: None)
            sender.send_emails(self.conn, self.s, sleep=lambda _x: None)
        self.assertEqual(m.call_count, 2)

    def test_dm_queue_is_manual(self):
        self.assertEqual(sender.queue_dms(self.conn), 4)
        self.assertTrue(all(d["status"] == "MANUAL_PENDING" for d in sender.dm_queue(self.conn)))
        sender.mark_dm_sent(self.conn, "INF_036")
        self.assertIn("MANUAL_SENT", [d["status"] for d in sender.dm_queue(self.conn)])


if __name__ == "__main__":
    unittest.main()
