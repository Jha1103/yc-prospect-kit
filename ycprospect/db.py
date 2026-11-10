"""SQLite is authoritative; exports and Google Sheets are optional views."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .core import canonical_company_url, now_utc

CURRENT_SCAN_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  slug TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  yc_url TEXT NOT NULL,
  batch TEXT,
  industry TEXT,
  location TEXT,
  website TEXT,
  description TEXT,
  status TEXT,
  team_size INTEGER,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  last_scanned TEXT,
  scan_error TEXT,
  scan_version INTEGER NOT NULL DEFAULT 0,
  scan_result TEXT,
  contact_page TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
  slug TEXT NOT NULL REFERENCES companies(slug),
  email TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'public_yc_page',
  source_url TEXT,
  status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','drafted','sent','skipped')),
  gmail_id TEXT,
  discovered_at TEXT NOT NULL,
  PRIMARY KEY(slug,email)
);
CREATE INDEX IF NOT EXISTS ix_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS ix_companies_last_scanned ON companies(last_scanned);
CREATE TABLE IF NOT EXISTS company_sources (
  slug TEXT NOT NULL REFERENCES companies(slug),
  source_type TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY(slug, source_type, source_name)
);
CREATE INDEX IF NOT EXISTS ix_company_sources_source ON company_sources(source_name);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    # Non-destructive migration for installations created before website scanning.
    existing_companies = {row[1] for row in db.execute("PRAGMA table_info(companies)")}
    if "scan_version" not in existing_companies:
        db.execute("ALTER TABLE companies ADD COLUMN scan_version INTEGER NOT NULL DEFAULT 0")
    if "scan_result" not in existing_companies:
        db.execute("ALTER TABLE companies ADD COLUMN scan_result TEXT")
    if "contact_page" not in existing_companies:
        db.execute("ALTER TABLE companies ADD COLUMN contact_page TEXT")
    existing_contacts = {row[1] for row in db.execute("PRAGMA table_info(contacts)")}
    if "source_url" not in existing_contacts:
        db.execute("ALTER TABLE contacts ADD COLUMN source_url TEXT")
    additional = {
        "source_type": "TEXT NOT NULL DEFAULT 'yc'",
        "source_name": "TEXT NOT NULL DEFAULT 'Y Combinator'",
        "profile_url": "TEXT",
        "founders": "TEXT",
        "founder_links": "TEXT",
        "careers_url": "TEXT",
        "outreach_status": "TEXT NOT NULL DEFAULT 'not_contacted'",
        "notes": "TEXT",
    }
    for col, definition in additional.items():
        if col not in existing_companies:
            db.execute(f"ALTER TABLE companies ADD COLUMN {col} {definition}")
    # Preserve old YC results as linked sources, without overwriting other origins.
    db.execute("""INSERT OR IGNORE INTO company_sources(slug,source_type,source_name,source_url,added_at)
                  SELECT slug,'yc','Y Combinator',yc_url,first_seen FROM companies
                  WHERE yc_url != '' AND (source_type='yc' OR source_type IS NULL)""")
    db.commit()
    return db


def upsert_companies(db: sqlite3.Connection, companies: list[dict]) -> int:
    current = now_utc()
    for item in companies:
        slug = item["slug"]
        try:
            url = canonical_company_url(slug)
        except ValueError:
            continue
        db.execute("""
            INSERT INTO companies(slug,name,yc_url,batch,industry,location,website,description,status,team_size,first_seen,last_seen)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET name=excluded.name,yc_url=excluded.yc_url,batch=excluded.batch,
            industry=excluded.industry,location=excluded.location,website=excluded.website,
            description=excluded.description,status=excluded.status,team_size=excluded.team_size,last_seen=excluded.last_seen
        """, (slug, str(item.get("name") or slug), url,
              item.get("batch") or "", item.get("industry") or "", item.get("all_locations") or "",
              item.get("website") or "", item.get("one_liner") or "", item.get("status") or "",
              item.get("team_size") if isinstance(item.get("team_size"), int) else None, current, current))
    db.execute("""INSERT OR IGNORE INTO company_sources(slug,source_type,source_name,source_url,added_at)
                  SELECT slug,'yc','Y Combinator',yc_url,first_seen FROM companies
                  WHERE yc_url != '' AND source_type='yc'""")
    db.commit()
    return len(companies)


def pending_scan_slugs(db: sqlite3.Connection, slugs: list[str], max_pages: int, retry_days: int) -> list[str]:
    if not slugs or max_pages <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retry_days)).isoformat(timespec="seconds")
    # SQLite variable limit safe: process batches without building a giant IN clause.
    found = []
    for start in range(0, len(slugs), 400):
        group = slugs[start:start + 400]
        marks = ",".join("?" for _ in group)
        found.extend(db.execute(f"SELECT slug,last_scanned,scan_version FROM companies WHERE slug IN ({marks}) AND (last_scanned IS NULL OR last_scanned <= ? OR scan_version < {CURRENT_SCAN_VERSION})", (*group, cutoff)).fetchall())
    return [row["slug"] for row in sorted(found, key=lambda r: (r["last_scanned"] is not None, r["last_scanned"] or "", r["slug"]))[:max_pages]]


def save_scan(db: sqlite3.Connection, slug: str, emails: list, error: str | None = None,
              result: str | None = None, contact_page: str | None = None) -> None:
    """Store a scan atomically, including source provenance and scanner version.

    Accepts historical plain-string emails and new EmailFinding objects.
    """
    current = now_utc()
    normalized = []
    for finding in emails:
        if isinstance(finding, str):
            normalized.append((finding, "public_yc_page", None))
        else:
            normalized.append((finding.email, finding.source, finding.source_url))
    outcome = result or ("error" if error else ("found" if normalized else "none"))
    db.execute("UPDATE companies SET last_scanned=?,scan_error=?,scan_version=?,scan_result=?,contact_page=COALESCE(?,contact_page) WHERE slug=?",
               (current, error[:500] if error else None, CURRENT_SCAN_VERSION, outcome, contact_page, slug))
    if error is None:
        for email, source, source_url in normalized:
            db.execute("""INSERT INTO contacts(slug,email,source,source_url,discovered_at)
                          VALUES(?,?,?,?,?) ON CONFLICT(slug,email) DO UPDATE SET
                          source=CASE WHEN contacts.source='public_yc_page' THEN excluded.source ELSE contacts.source END,
                          source_url=COALESCE(contacts.source_url, excluded.source_url)""",
                       (slug, email, source, source_url, current))
    db.commit()


def pending_outreach(db: sqlite3.Connection, limit: int, allowed_status: str = "new") -> list[sqlite3.Row]:
    return db.execute("""
      SELECT c.slug,c.email,c.status,co.name,co.batch,co.industry,co.website,co.description
      FROM contacts c JOIN companies co ON co.slug=c.slug
      WHERE c.status=?
        AND NOT EXISTS (SELECT 1 FROM contacts previous WHERE previous.email=c.email AND previous.status IN ('drafted','sent'))
        AND c.slug=(SELECT MIN(first.slug) FROM contacts first WHERE first.email=c.email AND first.status='new')
      ORDER BY c.discovered_at,c.slug,c.email LIMIT ?
    """, (allowed_status, limit)).fetchall()


def record_outreach(db: sqlite3.Connection, slug: str, email: str, status: str, gmail_id: str) -> None:
    cur = db.execute("UPDATE contacts SET status=?,gmail_id=? WHERE slug=? AND email=? AND status='new'", (status, gmail_id, slug, email))
    db.commit()
    if cur.rowcount != 1:
        raise RuntimeError("Contact state changed; refusing duplicate outreach")


def company_rows(db: sqlite3.Connection):
    return db.execute("""SELECT c.slug,c.name,c.batch,c.industry,c.location,c.website,c.description,c.status,
        c.team_size,c.yc_url,c.last_scanned,c.scan_error,c.scan_result,c.contact_page,
        (SELECT group_concat(email, '; ') FROM contacts ct WHERE ct.slug=c.slug) AS public_emails
        FROM companies c ORDER BY c.name COLLATE NOCASE""").fetchall()


def contact_rows(db: sqlite3.Connection):
    return db.execute("""SELECT c.name,c.slug,c.yc_url,c.batch,c.industry,c.location,c.website,
        ct.email,ct.source,ct.source_url,ct.status,ct.gmail_id,ct.discovered_at
        FROM contacts ct JOIN companies c ON c.slug=ct.slug ORDER BY c.name COLLATE NOCASE,ct.email""").fetchall()


def statistics(db: sqlite3.Connection) -> dict[str, int]:
    return {
        "companies": db.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
        "scanned_companies": db.execute("SELECT COUNT(*) FROM companies WHERE last_scanned IS NOT NULL AND scan_error IS NULL").fetchone()[0],
        "contacts": db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
        "new_contacts": db.execute("SELECT COUNT(*) FROM contacts WHERE status='new'").fetchone()[0],
        "gmail_drafts": db.execute("SELECT COUNT(*) FROM contacts WHERE status='drafted'").fetchone()[0],
        "sent": db.execute("SELECT COUNT(*) FROM contacts WHERE status='sent'").fetchone()[0],
        "scan_errors": db.execute("SELECT COUNT(*) FROM companies WHERE scan_error IS NOT NULL").fetchone()[0],
        "no_email": db.execute("SELECT COUNT(*) FROM companies WHERE scan_result IN ('none','no_website')").fetchone()[0],
        "blocked": db.execute("SELECT COUNT(*) FROM companies WHERE scan_result = 'blocked'").fetchone()[0],
    }


def upsert_external(db: sqlite3.Connection, rows: list[dict], source_type: str,
                    source_name: str, source_url: str) -> list[str]:
    """Import review-approved public startups; de-duplicate by company website hostname.

    Founders are explicitly supplied, never inferred from names. Never overwrite
    existing founder or URL fields with blank scraped data.
    """
    from urllib.parse import urlsplit
    from .sources import safe_web_url, company_key, SOURCE_TYPES
    if source_type not in SOURCE_TYPES:
        raise ValueError('Invalid source type')
    source_name = str(source_name or '').strip()[:120]
    if not source_name:
        raise ValueError('Investor, program or directory name required')
    source_url = safe_web_url(source_url) if source_url else ''
    current = now_utc()
    processed=[]
    existing_domains={}
    for existing in db.execute("SELECT slug, website FROM companies WHERE website != ''"):
        host=(urlsplit(existing['website']).hostname or '').removeprefix('www.')
        if host: existing_domains[host]=existing['slug']
    for item in rows[:1500]:
        name=str(item.get('name') or '').strip()[:150]
        if len(name) < 2:
            continue
        website=safe_web_url(str(item.get('website') or ''))
        profile=safe_web_url(str(item.get('profile_url') or ''))
        founder_links = str(item.get('founder_links') or '').strip()[:500]
        if founder_links:
            # Keep only web-based public profile URLs, not arbitrary text scripts.
            founder_links = '\n'.join(safe_web_url(part.strip()) for part in founder_links.replace(';','\n').splitlines() if part.strip())[:500]
        careers=safe_web_url(str(item.get('careers_url') or ''))
        slug=None
        domain=(urlsplit(website).hostname or '').removeprefix('www.')
        if domain:
            slug=existing_domains.get(domain)
        if not slug:
            slug=company_key(name,website,source_name)
        if domain:
            existing_domains[domain]=slug
        db.execute("""INSERT INTO companies(slug,name,yc_url,batch,industry,location,website,description,
                    status,team_size,first_seen,last_seen,source_type,source_name,profile_url,founders,founder_links,careers_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(slug) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    industry=CASE WHEN companies.industry='' THEN excluded.industry ELSE companies.industry END,
                    location=CASE WHEN companies.location='' THEN excluded.location ELSE companies.location END,
                    website=CASE WHEN companies.website='' THEN excluded.website ELSE companies.website END,
                    profile_url=COALESCE(NULLIF(companies.profile_url,''), excluded.profile_url),
                    founders=COALESCE(NULLIF(companies.founders,''), excluded.founders),
                    founder_links=COALESCE(NULLIF(companies.founder_links,''), excluded.founder_links),
                    careers_url=COALESCE(NULLIF(companies.careers_url,''), excluded.careers_url),
                    description=CASE WHEN companies.description='' THEN excluded.description ELSE companies.description END""",
                   (slug,name,'',str(item.get('batch') or '')[:120],str(item.get('industry') or '')[:150],
                    str(item.get('location') or '')[:150],website,str(item.get('description') or '')[:500],
                    str(item.get('status') or '')[:80],None,current,current,source_type,source_name,profile,
                    str(item.get('founders') or '')[:300],founder_links,careers))
        db.execute("""INSERT INTO company_sources(slug,source_type,source_name,source_url,added_at)
                      VALUES(?,?,?,?,?) ON CONFLICT(slug,source_type,source_name)
                      DO UPDATE SET source_url=CASE WHEN excluded.source_url!='' THEN excluded.source_url ELSE company_sources.source_url END""",
                   (slug,source_type,source_name,source_url,current))
        processed.append(slug)
    db.commit()
    return list(dict.fromkeys(processed))


def update_prospect(db: sqlite3.Connection, slug: str, changes: dict) -> bool:
    allowed={'outreach_status','founders','founder_links','careers_url','website','notes','location','industry'}
    values={k: str(v).strip()[:1000] for k,v in changes.items() if k in allowed and isinstance(v,str)}
    if not values: raise ValueError('No editable fields supplied')
    if 'outreach_status' in values and values['outreach_status'] not in ('not_contacted','shortlisted','contacted','replied','interviewing','closed'):
        raise ValueError('Invalid outreach status')
    if 'website' in values or 'careers_url' in values or 'founder_links' in values:
        from .sources import safe_web_url
        for key in ('website','careers_url'):
            if key in values: values[key]=safe_web_url(values[key])
        if 'founder_links' in values and values['founder_links']:
            values['founder_links']='\n'.join(safe_web_url(v.strip()) for v in values['founder_links'].replace(';','\n').splitlines() if v.strip())
    vals=list(values.values())
    sets=', '.join(f'{field}=?' for field in values)
    if 'website' in values:
        # A corrected website should be rescanned even if an earlier empty-site scan was cached.
        sets+=", last_scanned=NULL, scan_version=0, scan_result=NULL, scan_error=NULL"
    cur=db.execute('UPDATE companies SET '+sets+' WHERE slug=?',(*vals,slug))
    db.commit()
    return cur.rowcount > 0
