"""SQLite is authoritative; exports and Google Sheets are optional views."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .core import canonical_company_url, now_utc

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
  scan_error TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
  slug TEXT NOT NULL REFERENCES companies(slug),
  email TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'public_yc_page',
  status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','drafted','sent','skipped')),
  gmail_id TEXT,
  discovered_at TEXT NOT NULL,
  PRIMARY KEY(slug,email)
);
CREATE INDEX IF NOT EXISTS ix_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS ix_companies_last_scanned ON companies(last_scanned);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
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
        found.extend(db.execute(f"SELECT slug,last_scanned FROM companies WHERE slug IN ({marks}) AND (last_scanned IS NULL OR last_scanned <= ?)", (*group, cutoff)).fetchall())
    return [row["slug"] for row in sorted(found, key=lambda r: (r["last_scanned"] is not None, r["last_scanned"] or "", r["slug"]))[:max_pages]]


def save_scan(db: sqlite3.Connection, slug: str, emails: list[str], error: str | None = None) -> None:
    current = now_utc()
    db.execute("UPDATE companies SET last_scanned=?,scan_error=? WHERE slug=?", (current, error[:500] if error else None, slug))
    if error is None:
        for email in emails:
            db.execute("INSERT OR IGNORE INTO contacts(slug,email,discovered_at) VALUES(?,?,?)", (slug, email, current))
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
        c.team_size,c.yc_url,c.last_scanned,c.scan_error,
        (SELECT group_concat(email, '; ') FROM contacts ct WHERE ct.slug=c.slug) AS public_emails
        FROM companies c ORDER BY c.name COLLATE NOCASE""").fetchall()


def contact_rows(db: sqlite3.Connection):
    return db.execute("""SELECT c.name,c.slug,c.yc_url,c.batch,c.industry,c.location,c.website,
        ct.email,ct.source,ct.status,ct.gmail_id,ct.discovered_at
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
    }
