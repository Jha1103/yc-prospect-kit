# YC Prospect Kit

Small, independent Python tool for researching **publicly listed YC companies**, extracting **publicly visible contact addresses** on YC company pages, deduplicating into SQLite, exporting CSV, and optionally creating Gmail drafts or manually approved sends. It is **not** a verified founder-contact database and cannot discover private founder emails.

**Source:** Unofficial daily-updated [YC OSS dataset](https://github.com/yc-oss/api). This project is not affiliated with Y Combinator.

## What is included

- YC company directory URL filters: batch, industry, region(s), company status, stage, tags, team size, hiring and other boolean fields. Multiple values inside one filter are **OR**; different filter types are **AND**.
- Incremental company upserts by **slug**, with timestamped page scans and 30-day rescans.
- Public email extraction from YC HTML and `mailto:` links, limited to official YC company pages. Addresses are **unverified** and may be general company contacts.
- Local SQLite database, CSV exports, run statistics.
- Optional Gmail **drafts** (recommended) and explicit send mode capped at 10 per invocation, requiring terminal acknowledgement.
- Optional Google Sheets *snapshot export* into a dedicated worksheet. SQLite remains authoritative.
- No local web server, Chrome extension, background cron jobs, or paid API.

## 1. Setup (Mac / Linux)

```bash
git clone YOUR_NEW_GITHUB_REPOSITORY_URL
cd yc-prospect-kit
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If using the downloaded ZIP instead of GitHub, unzip it and `cd` into the project directory before creating the virtual environment.

## 2. Import YC companies and search for public contact addresses

```bash
python -m ycprospect sync --url "https://www.ycombinator.com/companies?batch=Fall%202026&industry=B2B" --max-pages 25
python -m ycprospect stats
python -m ycprospect export --type contacts --out data/contacts.csv
python -m ycprospect export --type companies --out data/companies.csv
```

`--max-pages 25` means up to **25 company pages** are inspected per run; **all matching company metadata** is imported. Repeat the same command to continue scanning the next companies. Use `--max-pages 0` for metadata-only import. The tool waits at least 1 second between requests and stores progress after every page.

If a URL contains an unsupported filter parameter, the CLI warns and ignores that parameter; review the count before using results. Successful page scans are not repeated for 30 days unless you use `--rescan-after-days 0`.

The data source is a third-party dataset and may lag YC's website. Scraping can fail due to robots restrictions, HTML changes, anti-bot controls, or lack of published addresses. Respect site terms and do not attempt to bypass controls.

## 3. Optional Gmail drafts (manual review is recommended)

```bash
python -m pip install -r requirements-google.txt
```

1. In Google Cloud Console, create a Google Cloud project and configure the OAuth consent screen.
2. Enable **Gmail API** (and **Google Sheets API** if using Sheets).
3. Create an **OAuth client ID → Desktop application** and save its downloaded client secrets as `credentials.json` in this project directory.
4. Authorize through the browser on the first Gmail command. The OAuth token is saved locally as `gmail_token.json` and excluded from Git.
5. Edit `message_example.txt` to your own relevant message. Use `{greeting}` and `{company}` placeholders; the greeting is company-based to avoid guessing someone’s identity.

Preview only (does not contact Gmail):

```bash
python -m ycprospect drafts --subject "Brief introduction" --body-file message_example.txt --limit 5 --dry-run
```

Create five **Gmail drafts**:

```bash
python -m ycprospect drafts --subject "Brief introduction" --body-file message_example.txt --limit 5
```

The tool remembers which `(company, email)` pairs have been drafted, so those addresses are excluded from subsequent runs. Review each draft and send from Gmail manually. It does **not** detect whether a user later sent a draft manually.

For deliberate API sends (not the default):

```bash
python -m ycprospect send --subject "Brief introduction" --body-file message_example.txt --limit 3 --confirm-send
```

The send command previews recipients and requires typing `SEND 3` in an interactive terminal. It is capped at 10 contacts per run and will not resend records already marked drafted or sent. Gmail accepting a send request does **not** guarantee delivery. Use only for relevant, lawful outreach; comply with applicable anti-spam rules and Google policies.

**Operational limitation:** As with other local SQLite + external API workflows, a process crash immediately after Gmail succeeds but before the SQLite status write can cause an attempted duplicate later. For high-volume or unattended campaigns, add reconciliation/idempotency beyond this starter tool. Do not run concurrent send processes.

## 4. Optional Google Sheets snapshot

```bash
python -m ycprospect sheets --sheet-id YOUR_SPREADSHEET_ID
```

Requires the `requirements-google.txt` dependencies and Google OAuth from step 3. Creates or replaces only the **`YC Prospect Export`** worksheet inside an existing Google Sheet; other tabs remain untouched. This is an export of companies plus semicolon-separated public emails, not bi-directional synchronization. To use a different worksheet, pass `--worksheet "My Export"`. Do not use a worksheet containing your own edits; this snapshot overwrites it.

## 5. Test

```bash
python -m unittest discover -s tests -v
```

No external network is required for tests. Live network access and Gmail/Sheets permissions are **not** exercised by the unit tests.

## Project structure

```text
ycprospect/
  core.py            # API fetch, URL filters and public email parser
  db.py              # SQLite records and deduplication
  google.py          # Optional Gmail/Sheets OAuth integrations
  cli.py             # One CLI; no workflow sprawl
  __main__.py        # python -m ycprospect entry point
tests/                # Local tests
requirements.txt
requirements-google.txt
message_example.txt
.gitignore
```

## Limits and deliberate omissions

- This version does **not** include the original project's Chrome extension. A second implementation of scraping/filter logic adds maintenance cost; a Chrome UI can be added later if users actually need it.
- It does **not** validate mailbox deliverability, confirm founder identity, guess emails, or scrape external company websites.
- Google Sheets is optional; no Google account or credentials are needed for collection/export.
- SQLite is suitable for a local single-user tool. **Vercel serverless filesystem is not persistent**; a future hosted dashboard should use managed Postgres and server-side OAuth credentials, not copy this local storage model directly.
- No company/client secrets, personal emails, or real prospect data should ever be committed to GitHub. Inspect exports before sharing publicly.
- This is an independently implemented starter, not a fork of the uploaded third-party repository. Check the original repository's license before copying its code.

## Publishing to your GitHub account

The GitHub API connection available in ChatGPT does not provide repository creation. The package is ready for a **new private GitHub repository**; no secrets or company data are included in the archive.

### Mac one-command route (GitHub CLI)

```bash
brew install gh
gh auth login
# Run from inside this project's folder after unpacking it:
./push_to_github.sh yc-prospect-kit
```

This initializes Git, commits the project, creates `yc-prospect-kit` as a **private** repository in your authenticated GitHub account, and pushes `main`. The script refuses to touch an existing Git remote.

### Manual route

Create a **new, empty private** repository in [GitHub](https://github.com/new) named `yc-prospect-kit` (do not initialize a README), then run inside this folder:

```bash
git init -b main
git add .
git commit -m "Initial YC Prospect Kit"
git remote add origin https://github.com/YOUR_USERNAME/yc-prospect-kit.git
git push -u origin main
```

To publish code, do **not** include `data/`, Google OAuth credentials, or token files.
