"""Single-entry CLI for collection, export, and optional Google integrations."""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import requests

from .core import company_matches, get_company_emails, load_company_feed, parse_directory_url
from .db import (company_rows, connect, contact_rows, pending_outreach,
                 pending_scan_slugs, record_outreach, save_scan, statistics, upsert_companies)

EXAMPLE_URL = "https://www.ycombinator.com/companies?batch=Fall%202026&industry=B2B"


def load_template(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("Message file is empty")
    return text


def email_body(template: str, row) -> str:
    # Conservative greeting: a public address doesn't prove a founder's name.
    company = row["name"]
    return template.replace("{company}", company).replace("{greeting}", f"{company} team")


def safe_excel_cell(value) -> str:
    text = str(value if value is not None else "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def command_sync(args, db) -> None:
    filters, ignored = parse_directory_url(args.url)
    if ignored:
        print("WARNING: unsupported YC URL parameters ignored:", ", ".join(ignored), file=sys.stderr)
    print("Loading YC OSS company feed...")
    session = requests.Session()
    source = load_company_feed(session)
    chosen = [company for company in source if company_matches(company, filters)]
    print(f"Feed records: {len(source)} | Matching filters: {len(chosen)}")
    if not chosen:
        return
    upsert_companies(db, chosen)
    slugs = [str(company["slug"]) for company in chosen]
    to_scan = pending_scan_slugs(db, slugs, args.max_pages, args.rescan_after_days)
    print(f"Stored companies: {len(chosen)} | Public YC pages to scan this run: {len(to_scan)}")
    if args.max_pages == 0:
        print("Metadata imported without email scanning (--max-pages 0).")
        return
    found = 0
    errors = 0
    for index, slug in enumerate(to_scan, 1):
        try:
            emails = get_company_emails(slug, session)
            save_scan(db, slug, emails)
            found += len(emails)
            print(f"[{index}/{len(to_scan)}] {slug}: {len(emails)} public email(s)")
        except (requests.RequestException, ValueError) as exc:
            errors += 1
            save_scan(db, slug, [], str(exc))
            print(f"[{index}/{len(to_scan)}] {slug}: failed ({str(exc)[:140]})", file=sys.stderr)
        if index < len(to_scan):
            time.sleep(args.delay)
    print(f"Finished. Email occurrences observed: {found}; page errors: {errors}.")
    print("Note: public email != verified founder email. Some YC pages publish none.")


def command_export(args, db) -> None:
    rows = company_rows(db) if args.type == "companies" else contact_rows(db)
    columns = [key for key in rows[0].keys()] if rows else (["slug", "name", "yc_url", "batch", "industry", "public_emails"] if args.type == "companies" else ["name", "slug", "email", "status"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows([safe_excel_cell(row[column]) for column in columns] for row in rows)
    print(f"Exported {len(rows)} {args.type} to {out}")


def command_outreach(args, db, send: bool) -> None:
    from .google import create_gmail_draft, gmail_service, send_gmail_message
    if send and not args.confirm_send:
        raise ValueError("Sending is disabled by default. Re-run with --confirm-send (maximum 10 contacts per command).")
    if send and args.limit > 10:
        raise ValueError("Sending limit is 10 per invocation. Review each batch.")
    if not args.subject.strip():
        raise ValueError("Subject must not be empty")
    template = load_template(args.body_file)
    recipients = pending_outreach(db, args.limit)
    if not recipients:
        print("No new contacts are pending. Existing drafted/sent contacts are excluded.")
        return
    print(f"{'SEND' if send else 'DRAFT'} preview ({len(recipients)} contacts):")
    for row in recipients:
        print(f"  {row['name']}: {row['email']}")
    print("\nFirst email:\n", "To:", recipients[0]["email"], "\nSubject:", args.subject, "\n", email_body(template, recipients[0]), sep="")
    if args.dry_run:
        print("Dry run only: Gmail not contacted and statuses unchanged.")
        return
    if send:
        # Separate in-terminal acknowledgement avoids accidental sending from a copied shell command.
        if not sys.stdin.isatty():
            raise ValueError("Sending requires an interactive terminal")
        ack = input(f"Type SEND {len(recipients)} to submit these emails through Gmail: ").strip()
        if ack != f"SEND {len(recipients)}":
            print("Cancelled. No emails sent.")
            return
    service = gmail_service()
    done = 0
    for row in recipients:
        try:
            body = email_body(template, row)
            if send:
                message_id = send_gmail_message(service, row["email"], args.subject, body)
                status = "sent"
            else:
                message_id = create_gmail_draft(service, row["email"], args.subject, body)
                status = "drafted"
            record_outreach(db, row["slug"], row["email"], status, message_id)
            done += 1
            print(f"{status.upper()}: {row['email']}")
        except Exception as exc:
            print(f"FAILED: {row['email']}: {exc}", file=sys.stderr)
        if send and done:
            time.sleep(2)
    print(f"Gmail {'accepted' if send else 'created'} {done}/{len(recipients)}. Gmail acceptance does not guarantee delivery.")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ycprospect", description="YC public company research + opt-in Gmail outreach")
    parser.add_argument("--db", default="data/prospects.db", help="Local SQLite database path (default: data/prospects.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="Import filtered YC companies and extract public emails")
    sync.add_argument("--url", default=EXAMPLE_URL, help="YC directory URL containing filters")
    sync.add_argument("--max-pages", type=int, default=25, help="Maximum individual YC pages to inspect; 0=metadata only")
    sync.add_argument("--delay", type=float, default=1.5, help="Seconds between company page requests (min 1)")
    sync.add_argument("--rescan-after-days", type=int, default=30, help="Revisit scanned pages after this many days")
    exports = sub.add_parser("export", help="Write companies or contacts to CSV")
    exports.add_argument("--type", choices=("companies", "contacts"), default="contacts")
    exports.add_argument("--out", default="data/contacts.csv")
    sub.add_parser("stats", help="Report local database progress")
    for kind in ("drafts", "send"):
        outreach = sub.add_parser(kind, help=("Create reviewable Gmail drafts" if kind == "drafts" else "Explicit Gmail send with limits"))
        outreach.add_argument("--subject", required=True)
        outreach.add_argument("--body-file", required=True)
        outreach.add_argument("--limit", type=int, default=5)
        outreach.add_argument("--dry-run", action="store_true")
        if kind == "send":
            outreach.add_argument("--confirm-send", action="store_true")
    sheets = sub.add_parser("sheets", help="Export SQLite snapshot to a dedicated Google Sheets worksheet")
    sheets.add_argument("--sheet-id", required=True, help="Existing Google Sheets spreadsheet ID")
    sheets.add_argument("--worksheet", default="YC Prospect Export", help="Worksheet replaced by export")
    return parser


def main(argv=None) -> int:
    args = make_parser().parse_args(argv)
    if args.command == "sync" and (args.max_pages < 0 or args.delay < 1 or args.rescan_after_days < 0):
        raise SystemExit("--max-pages >= 0, --delay >= 1, and --rescan-after-days >= 0 are required")
    if args.command in ("drafts", "send") and not (1 <= args.limit <= 25):
        raise SystemExit("--limit must be between 1 and 25")
    db = connect(args.db)
    try:
        if args.command == "sync":
            command_sync(args, db)
        elif args.command == "export":
            command_export(args, db)
        elif args.command == "stats":
            for metric, value in statistics(db).items():
                print(f"{metric:20} {value}")
        elif args.command in ("drafts", "send"):
            command_outreach(args, db, send=args.command == "send")
        elif args.command == "sheets":
            from .google import export_sheet
            count = export_sheet(db, args.sheet_id, args.worksheet)
            print(f"Exported {count} companies to worksheet {args.worksheet!r}")
        return 0
    except (ValueError, RuntimeError, OSError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

