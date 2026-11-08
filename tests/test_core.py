import unittest

from ycprospect.core import (
    canonical_company_url, company_matches, extract_public_emails, parse_directory_url,
    team_size_matches,
)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.company = {
            "slug": "acme", "name": "Acme AI", "batch": "Fall 2026", "industry": "B2B",
            "industries": ["B2B", "Analytics"], "regions": ["South Asia", "India"],
            "all_locations": "Bengaluru, India", "team_size": 22,
            "status": "Active", "stage": "Early", "tags": ["AI"], "tags_highlighted": [],
            "isHiring": True, "top_company": False, "nonprofit": False,
            "app_answers": None,
        }

    def test_filter_parsing_repeated_and_percent_encoded(self):
        f, ignored = parse_directory_url(
            "https://www.ycombinator.com/companies?batch=Fall%202026&batch=Summer%202026&industry=B2B&regions=India&isHiring=true&foo=x"
        )
        self.assertEqual(f["batch"], ["Fall 2026", "Summer 2026"])
        self.assertEqual(f["region"], ["India"])
        self.assertEqual(f["isHiring"], ["true"])
        self.assertEqual(ignored, ["foo"])

    def test_invalid_site_rejected(self):
        with self.assertRaises(ValueError):
            parse_directory_url("https://example.com/companies?batch=Fall%202026")
        with self.assertRaises(ValueError):
            canonical_company_url("https://example.com/")

    def test_all_filters_enforced(self):
        f = {"batch": ["Fall 2026"], "industry": ["Analytics"], "region": ["India"],
             "isHiring": ["true"], "status": ["Active"], "stage": ["Early"],
             "tags": ["AI"], "team_size": ["[5,50]"], "nonprofit": ["false"], "q": ["Acme"]}
        self.assertTrue(company_matches(self.company, f))
        for key in f:
            broken = dict(f)
            broken[key] = {"isHiring": ["false"], "nonprofit": ["true"],
                           "team_size": ["200+"], "q": ["missing"]}.get(key, ["missing"])
            self.assertFalse(company_matches(self.company, broken), key)

    def test_team_size_variants(self):
        for value in ("[5, 50]", "20-25", "10+", "22", '["5","1,000+"]'):
            self.assertTrue(team_size_matches(22, [value]), value)
        self.assertFalse(team_size_matches(22, ["1,000+"]))

    def test_email_detection_strips_hidden_scripts_and_platform(self):
        sample = '''<html><head><script>secret@example.com</script></head><body>
        <nav>nav@email.io</nav><p>Contact founder@example.com or SALES@example.com</p>
        <a href="mailto:team@acme.io?subject=hi">Email</a>
        <p>privacy@acme.io support@ycombinator.com</p></body></html>'''
        emails = extract_public_emails(sample)
        self.assertEqual(emails, ["founder@example.com", "sales@example.com", "team@acme.io"])


if __name__ == "__main__":
    unittest.main()
