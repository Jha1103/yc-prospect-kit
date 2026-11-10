"""Local, single-user YC Prospect Kit dashboard; no hosted services or external UI dependencies."""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from .cli import safe_excel_cell
from .core import company_matches, load_company_feed, parse_directory_url
from .discovery import scan_company_contacts
from .sources import preview_portfolio, parse_csv_rows, SOURCE_TYPES
from .db import connect, pending_scan_slugs, save_scan, statistics, upsert_companies, upsert_external, update_prospect

BASE_DIR = Path(__file__).resolve().parents[1]
ASSET_DIR = Path(__file__).resolve().parent / "webassets"
DEFAULT_DB = BASE_DIR / "data" / "prospects.db"
PAGE_LIMIT = 100
MAX_SCAN_PAGES = 250


def make_query(params: dict[str, str], state: "AppState") -> tuple[str, list[object]]:
    clauses: list[str] = []
    bindings: list[object] = []
    keyword = params.get("q", "").strip()[:150]
    if keyword:
        clauses.append("""(c.name LIKE ? OR c.description LIKE ? OR c.industry LIKE ? OR c.location LIKE ?
           OR c.slug LIKE ? OR c.founders LIKE ? OR c.source_name LIKE ? OR
           EXISTS (SELECT 1 FROM company_sources cs WHERE cs.slug=c.slug AND cs.source_name LIKE ?))""")
        bindings.extend([f"%{keyword}%"] * 8)
    batch = params.get("batch", "").strip()[:100]
    if batch:
        clauses.append("c.batch LIKE ?")
        bindings.append(f"%{batch}%")
    status = params.get("status", "").strip()[:80]
    if status:
        clauses.append("c.status = ?")
        bindings.append(status)
    source_type=params.get("source_type", "").strip()[:60]
    if source_type:
        clauses.append("EXISTS(SELECT 1 FROM company_sources cs WHERE cs.slug=c.slug AND cs.source_type=?)")
        bindings.append(source_type)
    source_name=params.get("source_name", "").strip()[:120]
    if source_name:
        clauses.append("EXISTS(SELECT 1 FROM company_sources cs WHERE cs.slug=c.slug AND cs.source_name=?)")
        bindings.append(source_name)
    outreach_status=params.get("outreach_status", "").strip()[:40]
    if outreach_status:
        clauses.append("c.outreach_status=?")
        bindings.append(outreach_status)
    if params.get('has_website') == '1':
        clauses.append("c.website != ''")
    if params.get("has_email") == "1":
        clauses.append("EXISTS(SELECT 1 FROM contacts t WHERE t.slug=c.slug)")
    if params.get("scope") == "last":
        # An empty/incomplete last run must never silently fall back to all saved rows.
        with state.lock:
            slugs = tuple(state.last_slugs)
        if not slugs:
            clauses.append("0=1")
        else:
            # SQLite host parameter limits vary; temp tables would require a single shared connection.
            # The feed can match many companies, so use batched SQL IN ranges joined by OR.
            chunks = [slugs[i:i + 400] for i in range(0, len(slugs), 400)]
            clauses.append("(" + " OR ".join("c.slug IN (" + ",".join("?" for _ in chunk) + ")" for chunk in chunks) + ")")
            for chunk in chunks:
                bindings.extend(chunk)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), bindings


def rows_for_dashboard(db: sqlite3.Connection, params: dict[str, str], state: "AppState") -> dict:
    page = max(1, min(100000, int(params.get("page", "1"))))
    limit = max(10, min(PAGE_LIMIT, int(params.get("limit", "25"))))
    where, bindings = make_query(params, state)
    total = db.execute("SELECT COUNT(*) FROM companies c" + where, bindings).fetchone()[0]
    sql = """SELECT c.slug,c.name,c.batch,c.industry,c.location,c.website,c.description,c.status,
       c.team_size,c.yc_url,c.last_scanned,c.scan_error,c.scan_result,c.contact_page,
       c.source_type,c.source_name,c.profile_url,c.founders,c.founder_links,c.careers_url,c.outreach_status,c.notes,
       (SELECT GROUP_CONCAT(cs.source_name, '; ') FROM company_sources cs WHERE cs.slug=c.slug) AS backed_by,
       (SELECT GROUP_CONCAT(cs.source_url, '; ') FROM company_sources cs WHERE cs.slug=c.slug AND cs.source_url!='') AS evidence_urls,
       (SELECT GROUP_CONCAT(t.email, '; ') FROM contacts t WHERE t.slug=c.slug) AS public_emails,
       (SELECT COUNT(*) FROM contacts t WHERE t.slug=c.slug) AS contact_count
       FROM companies c""" + where + " ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?"
    records = db.execute(sql, [*bindings, limit, (page - 1) * limit]).fetchall()
    return {"items": [dict(record) for record in records], "total": total,
            "page": page, "limit": limit, "pages": max(1, math.ceil(total / limit))}


def export_csv(db: sqlite3.Connection, params: dict[str, str], state: "AppState", kind: str) -> bytes:
    if kind not in ("companies", "contacts"):
        raise ValueError("Export type must be companies or contacts")
    where, bindings = make_query(params, state)
    if kind == "companies":
        columns = ["slug", "name", "batch", "industry", "location", "website", "description",
                   "status", "source_type", "source_name", "backed_by", "evidence_urls", "profile_url", "founders",
                   "founder_links", "careers_url", "outreach_status", "notes", "team_size", "yc_url",
                   "last_scanned", "scan_error", "scan_result", "contact_page", "public_emails"]
        sql = """SELECT c.slug,c.name,c.batch,c.industry,c.location,c.website,c.description,c.status,
            c.source_type,c.source_name,c.profile_url,c.founders,c.founder_links,c.careers_url,c.outreach_status,c.notes,
            c.team_size,c.yc_url,c.last_scanned,c.scan_error,c.scan_result,c.contact_page,
            (SELECT GROUP_CONCAT(cs.source_name,'; ') FROM company_sources cs WHERE cs.slug=c.slug) AS backed_by,
            (SELECT GROUP_CONCAT(cs.source_url,'; ') FROM company_sources cs WHERE cs.slug=c.slug AND cs.source_url!='') AS evidence_urls,
            (SELECT GROUP_CONCAT(t.email,'; ') FROM contacts t WHERE t.slug=c.slug) AS public_emails
            FROM companies c""" + where + " ORDER BY c.name COLLATE NOCASE"
    else:
        columns = ["name", "slug", "backed_by", "founders", "founder_links", "outreach_status", "yc_url",
                   "batch", "industry", "location", "website", "email", "source", "source_url", "contact_status", "discovered_at"]
        sql = """SELECT c.name,c.slug,c.yc_url,c.batch,c.industry,c.location,c.website,
            c.founders,c.founder_links,c.outreach_status,
            (SELECT GROUP_CONCAT(cs.source_name, '; ') FROM company_sources cs WHERE cs.slug=c.slug) AS backed_by,
            t.email,t.source,t.source_url,t.status AS contact_status,t.discovered_at
            FROM companies c JOIN contacts t ON t.slug=c.slug""" + where + " ORDER BY c.name COLLATE NOCASE,t.email"
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in db.execute(sql, bindings):
        writer.writerow([safe_excel_cell(row[col]) for col in columns])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


class AppState:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.last_slugs: tuple[str, ...] = ()
        self.job = self._idle()

    @staticmethod
    def _idle() -> dict:
        return {"status": "idle", "phase": "Ready", "feed_total": 0, "matched": 0,
                "scan_total": 0, "scanned": 0, "emails_found": 0, "errors": 0, "no_email": 0, "blocked": 0,
                "warnings": [], "logs": [], "url": "", "started_at": None, "finished_at": None}

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.job, "logs": list(self.job["logs"]), "warnings": list(self.job["warnings"]),
                    "last_run_count": len(self.last_slugs)}

    def update(self, **kwargs):
        with self.lock:
            self.job.update(kwargs)

    def log(self, message: str):
        with self.lock:
            self.job["logs"] = (self.job["logs"] + [message])[-16:]

    def cancel(self) -> bool:
        with self.lock:
            if self.job["status"] != "running":
                return False
            self.cancel_event.set()
            self.job["phase"] = "Stopping after the current request..."
            return True

    def start(self, url: str, max_pages: int, rescan_days: int) -> dict:
        filters, warnings = parse_directory_url(url)
        if not (0 <= max_pages <= MAX_SCAN_PAGES):
            raise ValueError(f"Pages to scan must be between 0 and {MAX_SCAN_PAGES}")
        if not (0 <= rescan_days <= 365):
            raise ValueError("Rescan interval must be between 0 and 365 days")
        with self.lock:
            if self.job["status"] == "running":
                raise RuntimeError("A collection is already running")
            self.cancel_event = threading.Event()
            self.last_slugs = ()
            self.job = self._idle()
            self.job.update({"status": "running", "phase": "Loading company feed...", "url": url,
                             "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "warnings": [f"Ignored unsupported URL parameter: {p}" for p in warnings]})
        threading.Thread(target=self._collect, args=(filters, max_pages, rescan_days), daemon=True).start()
        return self.snapshot()

    def start_external_scan(self, max_pages: int, rescan_days: int, source_name: str = '') -> dict:
        if not 0 <= max_pages <= MAX_SCAN_PAGES or not 0 <= rescan_days <= 365:
            raise ValueError('Invalid scan limit or rescan interval')
        with self.lock:
            if self.job['status']=='running':
                raise RuntimeError('A collection is already running')
            self.cancel_event=threading.Event()
            self.last_slugs=()
            self.job=self._idle()
            self.job.update({'status':'running','phase':'Preparing website scan...',
                             'started_at':datetime.now().astimezone().isoformat(timespec='seconds'),
                             'url':source_name or 'External startup websites'})
        threading.Thread(target=self._scan_external, args=(max_pages, rescan_days, source_name), daemon=True).start()
        return self.snapshot()

    def _scan_external(self, max_pages: int, rescan_days: int, source_name: str):
        db=None
        session=requests.Session()
        try:
            db=connect(str(self.db_path))
            sql="""SELECT DISTINCT c.slug FROM companies c
                   JOIN company_sources cs ON cs.slug=c.slug WHERE cs.source_type!='yc'"""
            parameters=[]
            if source_name:
                sql+=' AND cs.source_name=?'
                parameters.append(source_name)
            all_slugs=[r[0] for r in db.execute(sql,parameters)]
            pending=pending_scan_slugs(db, all_slugs, max_pages, rescan_days)
            self.update(matched=len(all_slugs),scan_total=len(pending),phase='Scanning startup websites...')
            with self.lock:
                self.last_slugs=tuple(all_slugs)
            for i,slug in enumerate(pending,1):
                if self.cancel_event.is_set():
                    self.update(status='cancelled',phase='Stopped; saved scans retained')
                    return
                item=dict(db.execute('SELECT * FROM companies WHERE slug=?',(slug,)).fetchone())
                try:
                    outcome=scan_company_contacts(item,session)
                    save_scan(db,slug,outcome.contacts,result=outcome.result,contact_page=outcome.contact_url)
                    with self.lock:
                        self.job['emails_found']+=len(outcome.contacts)
                        if outcome.result in ('none','no_website'): self.job['no_email']+=1
                        if outcome.result=='blocked': self.job['blocked']+=1
                    self.log(f"{item['name']}: {len(outcome.contacts)} public contact(s) [{outcome.result}]")
                except (requests.RequestException,ValueError) as exc:
                    save_scan(db,slug,[],str(exc))
                    with self.lock: self.job['errors']+=1
                    self.log(f"{item['name']}: {str(exc)[:100]}")
                self.update(scanned=i,phase=f'Scanned {i} of {len(pending)} startup websites')
                if i<len(pending) and self.cancel_event.wait(1.2):
                    self.update(status='cancelled',phase='Stopped; saved scans retained')
                    return
            self.update(status='complete',phase='Website scan complete')
        except Exception as exc:
            self.update(status='failed',phase='Website scan failed')
            self.log(f'Error: {type(exc).__name__}: {str(exc)[:300]}')
        finally:
            if db: db.close()
            session.close()
            self.update(finished_at=datetime.now().astimezone().isoformat(timespec='seconds'))

    def _collect(self, filters: dict[str, list[str]], max_pages: int, rescan_days: int):
        db = None
        session = requests.Session()
        try:
            feed = load_company_feed(session)
            self.update(feed_total=len(feed), phase="Applying filters...")
            selected = [item for item in feed if company_matches(item, filters)]
            self.update(matched=len(selected), phase="Saving matching companies...")
            db = connect(str(self.db_path))
            upsert_companies(db, selected)
            slugs = tuple(item["slug"] for item in selected if isinstance(item.get("slug"), str))
            with self.lock:
                self.last_slugs = slugs
            self.log(f"Feed: {len(feed):,} companies · {len(selected):,} match your filters")
            pending = pending_scan_slugs(db, list(slugs), max_pages, rescan_days)
            self.update(scan_total=len(pending), phase=("Scanning YC company pages..." if pending else "Finishing..."))
            for index, slug in enumerate(pending, 1):
                if self.cancel_event.is_set():
                    self.update(status="cancelled", phase="Stopped. Saved progress retained.")
                    return
                try:
                    company = next(item for item in selected if item["slug"] == slug)
                    scan = scan_company_contacts(company, session)
                    save_scan(db, slug, scan.contacts, result=scan.result, contact_page=scan.contact_url)
                    with self.lock:
                        self.job["emails_found"] += len(scan.contacts)
                        if scan.result in ("none", "no_website"):
                            self.job["no_email"] += 1
                        elif scan.result == "blocked":
                            self.job["blocked"] += 1
                    self.log(f"{slug}: {len(scan.contacts)} public business email(s) across {scan.checked} page(s) [{scan.result}]")
                    for message in scan.messages[:3]:
                        self.log(f"  {slug}: {message}")
                except (requests.RequestException, ValueError) as exc:
                    save_scan(db, slug, [], str(exc))
                    with self.lock:
                        self.job["errors"] += 1
                    self.log(f"{slug}: page error — {str(exc)[:100]}")
                self.update(scanned=index, phase=f"Scanned {index} of {len(pending)} pages")
                if index < len(pending) and self.cancel_event.wait(1.2):
                    self.update(status="cancelled", phase="Stopped. Saved progress retained.")
                    return
            self.update(status="complete", phase=("Metadata imported; no pages scanned" if max_pages == 0 else
                                                  "Collection complete"))
        except Exception as exc:
            self.update(status="failed", phase="Collection failed")
            self.log(f"Error: {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            if db is not None:
                db.close()
            session.close()
            self.update(finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "StartupProspectKit/2.0"

        def log_message(self, format, *args):
            # Avoid logging full query strings, which may include identifying research terms.
            return

        def _send(self, data: bytes, content_type="application/json; charset=utf-8", status=200, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, data, status=200):
            self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), status=status)

        def _error(self, detail, code=400):
            self._json({"error": str(detail)}, status=code)

        def _allowed_host(self) -> bool:
            host = self.headers.get("Host", "").split(":")[0].lower()
            return host in {"127.0.0.1", "localhost"}

        def do_GET(self):
            if not self._allowed_host():
                return self._error("Localhost access only", 403)
            parts = urlsplit(self.path)
            route = parts.path
            params = {key: values[-1] for key, values in parse_qs(parts.query).items()}
            if route in ("/", "/dashboard"):
                return self._send((ASSET_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            assets = {"/app.css": ("dashboard.css", "text/css; charset=utf-8"),
                      "/app.js": ("dashboard.js", "text/javascript; charset=utf-8")}
            if route in assets:
                filename, mime = assets[route]
                return self._send((ASSET_DIR / filename).read_bytes(), mime)
            if route == '/api/template.csv':
                template='name,website,founders,founder_links,industry,location,profile_url,careers_url,description\n'
                return self._send(template.encode(), 'text/csv; charset=utf-8', extra={
                    'Content-Disposition': 'attachment; filename="startup-import-template.csv"'})
            if route == "/api/health":
                return self._json({"status": "ok"})
            if route == "/api/job":
                return self._json(state.snapshot())
            if route in ("/api/stats", "/api/companies", "/api/export", "/api/facets"):
                db = connect(str(state.db_path))
                try:
                    if route == "/api/stats":
                        stats=statistics(db)
                        stats['external_companies']=db.execute("SELECT COUNT(*) FROM companies WHERE source_type!='yc'").fetchone()[0]
                        stats['sources']=db.execute('SELECT COUNT(DISTINCT source_name) FROM company_sources').fetchone()[0]
                        stats['shortlisted']=db.execute("SELECT COUNT(*) FROM companies WHERE outreach_status='shortlisted'").fetchone()[0]
                        return self._json(stats)
                    if route == "/api/facets":
                        batches = [row[0] for row in db.execute("SELECT DISTINCT batch FROM companies WHERE batch IS NOT NULL AND batch != '' ORDER BY batch DESC")]
                        names=[r[0] for r in db.execute('SELECT DISTINCT source_name FROM company_sources ORDER BY source_name COLLATE NOCASE')]
                        return self._json({"batches": batches, "sources": names})
                    if route == "/api/companies":
                        try:
                            return self._json(rows_for_dashboard(db, params, state))
                        except ValueError as exc:
                            return self._error(exc)
                    kind = params.get("type", "contacts")
                    try:
                        payload = export_csv(db, params, state, kind)
                    except ValueError as exc:
                        return self._error(exc)
                    return self._send(payload, "text/csv; charset=utf-8", extra={
                        "Content-Disposition": f'attachment; filename="startup-prospects-{kind}.csv"'})
                finally:
                    db.close()
            return self._error("Not found", 404)

        def do_POST(self):
            if not self._allowed_host():
                return self._error("Localhost access only", 403)
            # JSON POST + same-origin check prevent accidental submissions from other websites.
            origin = self.headers.get("Origin", "")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}",
                                         f"http://localhost:{self.server.server_port}"}:
                return self._error("Cross-origin requests are not allowed", 403)
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self._error("Expected application/json", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 1_250_000:
                    return self._error("Request body too large", 413)
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Request body must be a JSON object")
                route = urlsplit(self.path).path
                if route == '/api/source/preview':
                    return self._json(preview_portfolio(str(body.get('url') or '')))
                if route == '/api/source/import':
                    selected=body.get('items')
                    if not isinstance(selected,list) or len(selected)>200:
                        raise ValueError('Select at most 200 companies per import')
                    for item in selected:
                        if not isinstance(item,dict): raise ValueError('Invalid company row')
                    source_type=str(body.get('source_type') or 'vc')
                    if source_type not in SOURCE_TYPES: raise ValueError('Invalid source type')
                    db=connect(str(state.db_path))
                    try:
                        slugs=upsert_external(db,selected,source_type,str(body.get('source_name') or ''),str(body.get('source_url') or ''))
                    finally: db.close()
                    with state.lock: state.last_slugs=tuple(slugs)
                    return self._json({'imported':len(slugs),'message':'Imported. Use Scan websites to search for public business contacts.'})
                if route == '/api/source/csv':
                    text=str(body.get('csv') or '')
                    rows=parse_csv_rows(text)
                    source_type=str(body.get('source_type') or 'csv')
                    if source_type not in SOURCE_TYPES: raise ValueError('Invalid source type')
                    db=connect(str(state.db_path))
                    try:
                        slugs=upsert_external(db,rows,source_type,str(body.get('source_name') or 'CSV import'),str(body.get('source_url') or ''))
                    finally: db.close()
                    with state.lock: state.last_slugs=tuple(slugs)
                    return self._json({'imported':len(slugs)})
                if route == '/api/source/scan':
                    return self._json(state.start_external_scan(int(body.get('max_pages',25)),
                                                int(body.get('rescan_days',30)),
                                                str(body.get('source_name') or '')),202)
                if route == '/api/prospect/update':
                    slug=str(body.get('slug') or '')
                    changes=body.get('changes')
                    if not slug or not isinstance(changes,dict):
                        raise ValueError('Provide a company and its fields')
                    db=connect(str(state.db_path))
                    try: changed=update_prospect(db,slug,changes)
                    finally: db.close()
                    return self._json({'saved':changed}) if changed else self._error('Company not found',404)
                if route == "/api/collect":
                    result = state.start(body.get("url", ""), int(body.get("max_pages", 25)),
                                         int(body.get("rescan_days", 30)))
                    return self._json(result, status=202)
                if route == "/api/cancel":
                    return self._json({"cancel_requested": state.cancel()})
                return self._error("Not found", 404)
            except (ValueError, TypeError) as exc:
                return self._error(exc)
            except RuntimeError as exc:
                return self._error(exc, 409)
            except requests.RequestException as exc:
                return self._error(f'Website request failed: {str(exc)[:250]}', 502)

    return Handler


def create_server(host="127.0.0.1", port=8766, db_path: Path | None = None):
    if host != "127.0.0.1":
        raise ValueError("Dashboard is local-only and must bind to 127.0.0.1")
    state = AppState(db_path or DEFAULT_DB)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    return server, state


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start the local Startup Prospect Kit dashboard")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    server, _ = create_server(port=args.port, db_path=args.db)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Startup Prospect Kit Dashboard: {url}\nPress Control-C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
