import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ycprospect.cli import main


class CliIntegrationTests(unittest.TestCase):
    def test_sync_to_export_and_draft_preview_offline(self):
        companies = [{"slug": "sample", "name": "Sample AI", "industry": "B2B", "batch": "Fall 2026", "isHiring": True}]
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "store.db")
            export = str(Path(tmp) / "contacts.csv")
            template = Path(tmp) / "email.txt"
            template.write_text("Hi {greeting}, I saw {company}.")
            with patch("ycprospect.cli.load_company_feed", return_value=companies), patch("ycprospect.cli.get_company_emails", return_value=["team@sample.ai"]):
                self.assertEqual(main(["--db", db, "sync", "--url", "https://www.ycombinator.com/companies?industry=B2B", "--max-pages", "1"]), 0)
            self.assertEqual(main(["--db", db, "export", "--type", "contacts", "--out", export]), 0)
            with Path(export).open(encoding="utf-8-sig", newline="") as handle:
                data = list(csv.DictReader(handle))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["email"], "team@sample.ai")
            self.assertEqual(main(["--db", db, "drafts", "--subject", "Hello", "--body-file", str(template), "--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
