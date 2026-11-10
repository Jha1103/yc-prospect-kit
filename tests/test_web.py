"""Offline integration coverage for the local dashboard and exports."""
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from ycprospect.web import create_server
from ycprospect.discovery import ScanOutcome, EmailFinding


class WebTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.server, self.state = create_server(port=0, db_path=Path(self.folder.name) / "test.db")
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.folder.cleanup()

    def fetch(self, route, payload=None, origin=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if origin:
            headers["Origin"] = origin
        req = Request(self.url + route, data=data, headers=headers)
        with urlopen(req, timeout=3) as r:
            raw = r.read()
            return r.status, r.headers, json.loads(raw) if "application/json" in r.headers["Content-Type"] else raw

    def test_dashboard_assets_and_initial_state(self):
        code, hdr, html = self.fetch("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Company collector", html)
        self.assertIn(b"Paste YC URL", html)
        self.assertEqual(self.fetch("/api/stats")[2]["companies"], 0)
        self.assertEqual(self.fetch("/api/companies")[2]["total"], 0)
        self.assertIn(b"startCollection", self.fetch("/app.js")[2])
        self.assertIn(b".collector-layout", self.fetch("/app.css")[2])

    def test_collect_filter_results_and_exports(self):
        feed = [
            {"slug": "india-b2b", "name": "SignalDesk", "batch": "Winter 2025", "industry": "B2B", "all_locations": "Bangalore, India", "isHiring": True},
            {"slug": "usa-b2b", "name": "SignalUSA", "batch": "Winter 2025", "industry": "B2B", "all_locations": "San Francisco, USA", "isHiring": True},
            {"slug": "india-consumer", "name": "PlayNow", "batch": "Summer 2024", "industry": "Consumer", "all_locations": "India", "isHiring": False},
        ]
        url = "https://www.ycombinator.com/companies?industry=B2B&region=India&isHiring=true&unused=ignored"
        with patch("ycprospect.web.load_company_feed", return_value=feed), patch("ycprospect.web.scan_company_contacts", return_value=ScanOutcome(contacts=[EmailFinding("team@signaldesk.ai", "company_contact", "https://signaldesk.ai/contact")], checked=3, result="found")):
            code, _, _ = self.fetch("/api/collect", {"url": url, "max_pages": 1, "rescan_days": 30})
            self.assertEqual(code, 202)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.fetch("/api/job")[2]
                if job["status"] != "running":
                    break
                time.sleep(.03)
            self.assertEqual(job["status"], "complete", job)
            self.assertEqual(job["matched"], 1)
            self.assertEqual(job["scanned"], 1)
            self.assertEqual(job["emails_found"], 1)
            self.assertEqual(job["last_run_count"], 1)
            self.assertTrue(job["warnings"])
            self.assertEqual(self.fetch("/api/stats")[2]["contacts"], 1)
            self.assertEqual(self.fetch("/api/facets")[2]["batches"], ["Winter 2025"])
            rows = self.fetch("/api/companies?scope=last&has_email=1")[2]
            self.assertEqual(rows["total"], 1)
            self.assertEqual(rows["items"][0]["name"], "SignalDesk")
            export = self.fetch("/api/export?type=contacts&scope=last")[2].decode("utf-8-sig")
            parsed = list(csv.DictReader(io.StringIO(export)))
            self.assertEqual(parsed[0]["email"], "team@signaldesk.ai")
            self.assertEqual(parsed[0]["source_url"], "https://signaldesk.ai/contact")
            self.assertEqual(len(parsed), 1)
            self.assertEqual(self.fetch("/api/export?type=companies&batch=Summer")[2].count(b"SignalDesk"), 0)

    def test_reject_invalid_url_and_out_of_range_scan(self):
        for payload in [
            {"url": "https://evil.example.com/companies", "max_pages": 20},
            {"url": "http://www.ycombinator.com/companies", "max_pages": 20},
            {"url": "https://www.ycombinator.com/companies", "max_pages": 9999},
        ]:
            with self.assertRaises(HTTPError) as context:
                self.fetch("/api/collect", payload)
            self.assertEqual(context.exception.code, 400)
        with self.assertRaises(HTTPError) as context:
            self.fetch("/api/collect", {"url": "https://www.ycombinator.com/companies"}, origin="https://evil.example")
        self.assertEqual(context.exception.code, 403)

    def test_repeated_scan_skipped_by_rescan_window(self):
        feed = [{"slug": "acme", "name": "Acme", "industry": "B2B"}]
        with patch("ycprospect.web.load_company_feed", return_value=feed), patch("ycprospect.web.scan_company_contacts", return_value=ScanOutcome(contacts=[EmailFinding("hi@acme.co", "company_home", "https://acme.co/")], checked=2, result="found")) as scans:
            for _ in range(2):
                self.fetch("/api/collect", {"url": "https://www.ycombinator.com/companies?industry=B2B", "max_pages": 25})
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if self.fetch("/api/job")[2]["status"] == "complete":
                        break
                    time.sleep(.02)
            self.assertEqual(scans.call_count, 1)
            self.assertEqual(self.fetch("/api/stats")[2]["contacts"], 1)

if __name__ == "__main__":
    unittest.main()
