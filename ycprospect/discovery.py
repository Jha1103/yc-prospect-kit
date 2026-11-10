"""Bounded, respectful discovery of publicly posted company contact emails.

YC company metadata supplies company websites, not founder emails. This scanner checks
one YC profile and at most four first-party website pages. No guessing email addresses,
search-engine scraping, credential use, or automatic outreach.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .core import USER_AGENT, canonical_company_url, extract_public_emails

MAX_SITE_PAGES = 4  # Home plus up to three clearly relevant pages.
MAX_CONTENT_BYTES = 2_000_000
CONTACT_PATHS = ("contact", "about", "team")
REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}


@dataclass(frozen=True)
class EmailFinding:
    email: str
    source: str
    source_url: str


@dataclass
class ScanOutcome:
    contacts: list[EmailFinding] = field(default_factory=list)
    checked: int = 0
    messages: list[str] = field(default_factory=list)
    result: str = "none"
    contact_url: str | None = None


def _public_url(raw: str) -> str:
    """Reject local/private network hosts and non-web schemes before an HTTP request."""
    raw = (raw or "").strip()
    if raw and not urlsplit(raw).scheme:
        raw = "https://" + raw
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Missing or invalid public company website URL")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Nonstandard website port is blocked")
    host = parsed.hostname.rstrip(".").lower()
    if not host or "." not in host or host.endswith((".local", ".internal", ".localhost", ".test", ".invalid")):
        raise ValueError("Local or non-public host blocked")
    if re.search(r"[^a-z0-9.\-]", host) or ".." in host:
        raise ValueError("Invalid company website hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Direct IP website URLs are blocked")
    # Validate hostname resolution. This prevents ordinary localhost/private-address SSRF;
    # network changes between DNS check and request remain a residual risk.
    try:
        addrs = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Company website DNS unavailable: {host}") from exc
    if not addrs or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addrs):
        raise ValueError("Website resolves to a non-public IP address")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _get_html(session: requests.Session, url: str, allowed_hosts: set[str] | None = None) -> tuple[str, str]:
    """Handle redirects explicitly, validating destination on every hop."""
    current = url
    for _ in range(4):
        current = _public_url(current)
        host = urlsplit(current).hostname or ""
        if allowed_hosts is not None and host not in allowed_hosts:
            raise ValueError("Website redirected to a different host")
        response = session.get(current, headers=REQUEST_HEADERS, timeout=(6, 12), allow_redirects=False, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Website redirect without a destination")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            if "html" not in response.headers.get("Content-Type", "text/html").lower():
                raise ValueError("Website response is not HTML")
            length = int(response.headers.get("Content-Length") or 0)
            if length > MAX_CONTENT_BYTES:
                raise ValueError("Website HTML exceeds size limit")
            chunks, used = [], 0
            for chunk in response.iter_content(64 * 1024):
                used += len(chunk)
                if used > MAX_CONTENT_BYTES:
                    raise ValueError("Website HTML exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), current
        finally:
            response.close()
    raise ValueError("Too many redirects")


def _robots_allowed(session: requests.Session, url: str, cache: dict | None = None) -> bool:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    cache = {} if cache is None else cache
    if robots_url not in cache:
        try:
            raw, _ = _get_robots(session, robots_url, parts.hostname or "")
        except (requests.RequestException, ValueError):
            # Do not crawl when site policy cannot be checked.
            cache[robots_url] = False
        else:
            if raw is None:
                cache[robots_url] = True
            else:
                parser = RobotFileParser()
                parser.parse(raw.splitlines())
                cache[robots_url] = parser
    policy = cache[robots_url]
    return policy if isinstance(policy, bool) else policy.can_fetch(USER_AGENT, url)


def _get_robots(session: requests.Session, url: str, expected_host: str) -> tuple[str | None, str]:
    """Follow bounded http->https or www redirects without leaving the site."""
    allowed_hosts = _host_set(expected_host)
    current = url
    for _ in range(4):
        safe = _public_url(current)
        if urlsplit(safe).hostname not in allowed_hosts:
            raise ValueError("robots.txt redirected to another website")
        response = session.get(safe, headers=REQUEST_HEADERS, timeout=(4, 8), allow_redirects=False, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("robots.txt redirect missing destination")
                current = urljoin(safe, location)
                continue
            if response.status_code in (404, 410):
                return None, safe
            if response.status_code in (401, 403):
                return "User-agent: *\nDisallow: /", safe
            response.raise_for_status()
            return response.raw.read(100_001, decode_content=True)[:100_000].decode("utf-8", "replace"), safe
        finally:
            response.close()
    raise ValueError("Too many redirects while checking robots.txt")


def _host_set(host: str) -> set[str]:
    h = host.lower().removeprefix("www.")
    return {h, "www." + h}


def _first_party_links(html: str, base: str, hosts: set[str]) -> list[tuple[str, str]]:
    """Pick one contact/about/team link each, never wander through unrelated pages."""
    soup = BeautifulSoup(html, "html.parser")
    choices = {}
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("https", "http") or parts.hostname not in hosts:
            continue
        path = parts.path.lower().strip("/")
        label = (path + " " + a.get_text(" ", strip=True).lower())[:150]
        for key in CONTACT_PATHS:
            if key in label and key not in choices and len(path) <= 90:
                choices[key] = (key, urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
                break
    if "contact" not in choices:
        choices["contact"] = ("contact", urljoin(base, "/contact"))
    return [choices[k] for k in CONTACT_PATHS if k in choices][: MAX_SITE_PAGES - 1]


def scan_company_contacts(company: dict, session: requests.Session | None = None) -> ScanOutcome:
    outcome = ScanOutcome()
    session = session or requests.Session()
    seen_emails: set[str] = set()
    robots_cache: dict = {}

    def save(html: str, url: str, source: str):
        outcome.checked += 1
        for email in extract_public_emails(html, include_layout=(source != "yc_profile")):
            if email not in seen_emails:
                seen_emails.add(email)
                outcome.contacts.append(EmailFinding(email, source, url))

    slug = str(company.get("slug") or "")
    # Only visit a YC profile when this is a verified YC company record.
    if company.get('source_type', 'yc') == 'yc':
        try:
            yc_url = canonical_company_url(slug)
            yc_html, yc_final = _get_html(session, yc_url, _host_set("ycombinator.com"))
            save(yc_html, yc_final, "yc_profile")
        except (requests.RequestException, ValueError) as exc:
            outcome.messages.append(f"YC profile unavailable: {str(exc)[:90]}")

    website = str(company.get("website") or "").strip()
    if not website:
        outcome.messages.append("No company website available for this startup")
        outcome.result = "found" if outcome.contacts else "no_website"
        return outcome

    try:
        home = _public_url(website)
        hosts = _host_set(urlsplit(home).hostname or "")
        if not _robots_allowed(session, home, robots_cache):
            outcome.messages.append("Company website blocked by robots policy or robots check failed")
            outcome.result = "found" if outcome.contacts else "blocked"
            return outcome
        html, final = _get_html(session, home, hosts)
        save(html, final, "company_home")
        links = _first_party_links(html, final, hosts)
        visited = {final.rstrip("/")}
        for source, link in links:
            if link.rstrip("/") in visited:
                continue
            visited.add(link.rstrip("/"))
            if not _robots_allowed(session, link, robots_cache):
                outcome.messages.append(f"Skipped disallowed {source} page")
                continue
            try:
                next_html, resolved = _get_html(session, link, hosts)
                save(next_html, resolved, f"company_{source}")
                if source == "contact":
                    outcome.contact_url = resolved
            except (requests.RequestException, ValueError) as exc:
                outcome.messages.append(f"{source} page unavailable: {str(exc)[:80]}")
    except (requests.RequestException, ValueError) as exc:
        outcome.messages.append(f"Company website unavailable: {str(exc)[:110]}")
        outcome.result = "found" if outcome.contacts else "blocked"
        return outcome
    outcome.result = "found" if outcome.contacts else "none"
    return outcome
