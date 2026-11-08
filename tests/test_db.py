import tempfile
import unittest
from pathlib import Path

from ycprospect.db import connect, contact_rows, pending_outreach, pending_scan_slugs, record_outreach, save_scan, statistics, upsert_companies


class DbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.temp.name) / "test.sqlite"))
        self.companies = [
            {"slug": "alpha", "name": "Alpha", "batch": "Fall 2026", "industry": "B2B"},
            {"slug": "beta", "name": "Beta", "batch": "Fall 2026", "industry": "B2B"},
        ]

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_upsert_scan_rescan_status(self):
        upsert_companies(self.db, self.companies)
        upsert_companies(self.db, self.companies)
        self.assertEqual(statistics(self.db)["companies"], 2)
        self.assertEqual(len(pending_scan_slugs(self.db, ["alpha", "beta"], 10, 30)), 2)
        save_scan(self.db, "alpha", ["x@alpha.com"])
        save_scan(self.db, "alpha", ["x@alpha.com"])
        self.assertEqual(statistics(self.db)["contacts"], 1)
        self.assertEqual(pending_scan_slugs(self.db, ["alpha", "beta"], 10, 30), ["beta"])
        self.assertEqual(len(pending_outreach(self.db, 10)), 1)
        record_outreach(self.db, "alpha", "x@alpha.com", "drafted", "gmail-id")
        self.assertEqual(statistics(self.db)["gmail_drafts"], 1)
        self.assertEqual(len(pending_outreach(self.db, 10)), 0)
        with self.assertRaises(RuntimeError):
            record_outreach(self.db, "alpha", "x@alpha.com", "sent", "dup")

    def test_email_deduped_across_companies(self):
        upsert_companies(self.db, self.companies)
        save_scan(self.db, "alpha", ["shared@example.com"])
        save_scan(self.db, "beta", ["shared@example.com"])
        self.assertEqual(len(contact_rows(self.db)), 2)
        pending = pending_outreach(self.db, 10)
        self.assertEqual(len(pending), 1)
        record_outreach(self.db, pending[0]["slug"], pending[0]["email"], "drafted", "x")
        self.assertEqual(len(pending_outreach(self.db, 10)), 0)


if __name__ == "__main__":
    unittest.main()
