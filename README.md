# Startup Prospect Kit — multi-source dashboard

Local startup research application for YC, venture capital portfolios, accelerator cohorts, incubators, and your own CSV lists. It preserves the original Python module name `ycprospect` for compatibility with older installs and Local App Hub.

## Launch

Start from your existing **Local App Hub** (previously listed as **YC Prospect Kit**) or run:

```bash
cd "$HOME/Downloads/yc-prospect-kit"
/bin/bash "Start YC Prospect Kit.command"
```

Open <http://127.0.0.1:8766>. **No Bash commands are required for ordinary collection and imports.**

## Four discovery modes

1. **Y Combinator:** use directory filters or paste an HTTPS YC companies URL. The YC OSS dataset supplies metadata. Filter on `region=India` to target India-based companies, not nationality.
2. **VC / Accelerator:** paste one public portfolio/cohort URL. The scanner extracts likely links and presents a selectable **review list**. **Candidate links are not verified investments, startups, or founder identities.** Review them before importing.
3. **CSV upload:** import `name, website, founders, founder_links, industry, location, profile_url, careers_url, description`. `name` is the only mandatory column. The dashboard has a downloadable template. Supports up to 1 MB and 1,500 rows per import.
4. **Manual startup:** add a verified company, source name, and optional founder profile.

Investor/program names are stored as attribution; one startup can have multiple investor sources. Websites are de-duplicated by hostname when available, including against YC records. A company with no website can be added but cannot be scanned until a website is supplied.

Source examples (not guaranteed to be directly machine-readable):

- [Elevation Capital portfolio](https://www.elevationcapital.com/portfolio)
- [Peak XV companies](https://www.peakxv.com/our-companies)
- [Antler portfolio](https://www.antler.co/portfolio)

**Portfolio scanning is deliberately conservative**. It reads a single public page, obeys robots policy, and extracts HTML links; it does **not** crawl a fund's entire website, bypass JS/login/anti-bot measures, or automatically infer nationalities. Some directories publish only client-side content: preview may show zero or noisy candidates. Use a downloaded, authorized CSV or manually add a company in that case.

## Collect contacts

1. Import the companies using a mode above.
2. Use **Scan imported websites** from the activity panel, optionally selecting one source. Or YC mode's **Start YC collection** scans a selected number of YC company pages.
3. The scanner visits the company homepage and up to three first-party contact/about/team links, if allowed by robots.txt. It extracts **publicly published business email addresses**, not private founder contacts. Click the public source links on the results table.
4. In the results table, filter by investor/program, source type, outreach status, or public email. Use **Edit details** to attach founder names/links after verifying them from public profiles.
5. Export **Companies CSV** (all matching companies with founder/source/outreach fields) or **Contacts CSV** (emails with source URLs). Nothing is automatically sent.

## Storage, security, and privacy

- Original database remains `data/prospects.db`. On first launch, extra columns and `company_sources` table are added without deleting records or contact states.
- Bind only to `127.0.0.1`, validate the `Host` header, validate POST origins and require JSON. The dashboard is intended for a single user on one Mac; do not forward its port to the internet.
- External imports use syntactically validated public URLs. Before any HTTP website fetch, the collector checks DNS, HTTP redirects, permitted schemes, addresses and robots.txt. Public webpages and directories can change or block scraping.
- All user-edited/outside-source values are escaped in the browser and exported with formula-injection protection.
- Do not interpret missing public email as proof of no founder contact route.
- Do not mass-email: use individually reviewed job-relevant outreach and respect site terms and applicable privacy and anti-spam law.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

Tests run without live YC or portfolio requests. Site-specific extraction performance has **not** been validated against all live portfolio pages.

## Existing CLI

The original `python -m ycprospect sync`, `stats`, `export`, and optional Gmail draft commands remain available. They operate on the same SQLite database. This dashboard intentionally does not add automatic email sending.

## Publishing code to GitHub

From your existing project folder, run:

```bash
/bin/bash "Publish Dashboard Update.command"
```

The script stages only application code/docs/tests and pushes to your configured GitHub repository; it does not stage `data/`, OAuth tokens, `.env` files, or local database files.
