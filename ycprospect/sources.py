"""Conservative, review-first import from public investor and accelerator portfolio pages.

A directory URL is not an API: different sites use different markup, many use JS.
Candidates are suggestions for human review, never verified investments or founders.
"""
from __future__ import annotations

import csv
import hashlib
import ipaddress
import io
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .discovery import _get_html, _host_set, _public_url, _robots_allowed

SOURCE_TYPES = {'vc', 'accelerator', 'incubator', 'directory', 'manual', 'csv'}
BLOCKED_HOST_PARTS = (
    'facebook.com', 'instagram.com', 'linkedin.com', 'twitter.com', 'x.com',
    'youtube.com', 'tiktok.com', 'wa.me', 'github.com', 'medium.com',
    'notion.so', 'calendly.com', 'google.com', 'apple.com', 'play.google.com',
)
NAV_LABELS = {
    'home','about','team','careers','jobs','contact','apply','companies',
    'portfolio','our portfolio','our companies','investments','invest',
    'blog','stories','news','privacy','terms','login','sign in','sign up',
    'view all','see all','discover more','read more','learn more',
    'click here','explore','next','previous','support','faq','menu','events',
}
PORTFOLIO_HINTS = ('portfolio','companies','startups','investment','venture','cohort','backed','founders')


def safe_web_url(value: str) -> str:
    """Validate a stored URL syntactically; DNS checks occur immediately before fetching."""
    raw = str(value or '').strip()
    if len(raw) > 2048:
        raise ValueError('URL is too long')
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    parsed = urlsplit(raw)
    host = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme not in ('https', 'http') or not host or parsed.username or parsed.password:
        raise ValueError('Expected a public HTTPS/HTTP URL')
    if parsed.port not in (None, 80, 443) or '.' not in host or '..' in host:
        raise ValueError('Invalid public website host or port')
    if host.endswith(('.local','.localhost','.internal','.test','.invalid')) or not re.fullmatch(r'[a-z0-9.-]+', host):
        raise ValueError('Local or invalid website host')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Direct IP website URLs are blocked')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', parsed.query, ''))


def company_key(name: str, website: str, source_name: str = '') -> str:
    host = urlsplit(website).hostname or ''
    identity = 'domain:' + host.removeprefix('www.') if host else 'name:' + re.sub(r'\s+', ' ', name).casefold().strip() + ':source:' + source_name.casefold().strip()
    return 'external-' + hashlib.sha256(identity.encode()).hexdigest()[:20]


def _clean_name(name: str) -> str:
    name = re.sub(r'\s+', ' ', name or '').strip(' .·|\n\r\t')
    return name[:120]


def _is_name(name: str) -> bool:
    small = name.casefold()
    if not 2 <= len(name) <= 72 or len(name.split()) > 8 or small in NAV_LABELS:
        return False
    if any(bad in small for bad in ('cookie policy', 'all rights reserved', 'subscribe',
                                     'newsletter', 'our portfolio', 'privacy policy', 'terms of use',
                                     'read more', 'learn more', 'view all', 'follow us')):
        return False
    if not any(ch.isalpha() for ch in name):
        return False
    return True


def _extract_founders(parent) -> str:
    """Only copy explicitly founder-labelled names; do not infer identities."""
    text = parent.get_text(' ', strip=True)
    m = re.search(r'\b(?:Co-)?Founders?\s*:\s*(.{3,110}?)(?:\b(?:Industry|Sector|Stage|Location|Invested|Read more|Learn more)\b|$)', text, re.I)
    if not m:
        return ''
    value = _clean_name(m.group(1))
    return value[:120] if len(value) <= 120 else ''


def parse_portfolio_html(html: str, source_url: str, limit: int = 200) -> list[dict]:
    """Extract likely startup links, with explicit low-confidence review requirement.

    A page may use JS, absent anchors, generic 'Read more' links, or duplicate nav.
    In those cases users can upload a CSV or manually add companies.
    """
    soup = BeautifulSoup(html, 'html.parser')
    for item in soup(['script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header']):
        item.decompose()
    origin = urlsplit(source_url)
    base_host = (origin.hostname or '').removeprefix('www.')
    result = {}
    for a in soup.select('a[href]'):
        href = str(a.get('href') or '').strip()
        if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            continue
        link = urljoin(source_url, href)
        p = urlsplit(link)
        if p.scheme not in ('http','https') or not p.hostname:
            continue
        hostname = p.hostname.removeprefix('www.')
        if hostname.endswith(BLOCKED_HOST_PARTS) or hostname == 'ycombinator.com':
            continue
        if not p.hostname.replace('.', '').replace('-', '').isalnum():
            continue
        name = _clean_name(a.get_text(' ', strip=True))
        if not _is_name(name):
            img = a.find('img', alt=True)
            name = _clean_name(str(img.get('alt', '')).replace(' Logo', '').replace(' logo', '')) if img else ''
        if not _is_name(name):
            # Some cards wrap a "Read more" link but put the company in a sibling heading.
            parent = a.find_parent(['article', 'li', 'section']) or a.parent
            heading = parent.find(['h2','h3','h4']) if parent else None
            if heading:
                name = _clean_name(heading.get_text(' ',strip=True))
        if not _is_name(name):
            continue
        external = hostname != base_host
        path = p.path.lower()
        parent = a.find_parent(['article','li']) or a.parent
        cardclass = ' '.join(parent.get('class', [])).lower() if parent else ''
        context = (' '.join(a.get('class', [])).lower() + ' ' + cardclass + ' ' + path)
        is_profile = bool(any(hint in context for hint in PORTFOLIO_HINTS))
        if not external and not is_profile:
            # Prevent unrelated About/Blog/etc. links on the investor's own site.
            continue
        # Homepage links should never point to VC website itself.
        website = urlunsplit((p.scheme, p.netloc, p.path or '/', '', '')) if external else ''
        profile = urlunsplit((p.scheme, p.netloc, p.path or '/', '', '')) if not external else ''
        # Ignore repeated links to the same candidate (key on identity not URL).
        key = name.casefold()
        record = {'name': name, 'website': website, 'profile_url': profile, 'industry': '',
                  'location': '', 'founders': _extract_founders(parent) if parent else '', 'selected': True}
        old = result.get(key)
        if not old or (website and not old['website']):
            result[key] = record
        if len(result) >= limit:
            break
    return list(result.values())


def preview_portfolio(url: str, session: requests.Session | None = None) -> dict:
    url = safe_web_url(url)
    if not url:
        raise ValueError('Enter a portfolio page URL')
    client = session or requests.Session()
    host = urlsplit(url).hostname or ''
    if not _robots_allowed(client, url):
        raise ValueError('Source page disallows automated access, or robots.txt could not be checked. Use CSV import.')
    html, final = _get_html(client, url, _host_set(host))
    items = parse_portfolio_html(html, final)
    return {'items': items, 'count': len(items), 'url': final,
            'notice': ('These are link-based candidates, not verified portfolio memberships. Review before importing. '
                       'A missing company may be rendered in JavaScript; use CSV import or manual entry.')}


def parse_csv_rows(text: str, limit: int = 1500) -> list[dict]:
    if not isinstance(text, str) or len(text.encode('utf-8')) > 1_000_000:
        raise ValueError('CSV must be UTF-8 text and less than 1 MB')
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not reader.fieldnames:
        raise ValueError('CSV has no header row')
    aliases = {'company':'name','company_name':'name','startup':'name','startup_name':'name',
               'url':'website','company_url':'website','website_url':'website',
               'sector':'industry','program':'source_name','investor':'source_name',
               'vc':'source_name','city':'location','founder':'founders','founder_name':'founders',
               'founder_link':'founder_links','linkedin':'founder_links',
               'profile':'profile_url','company_profile':'profile_url',
               'jobs_url':'careers_url','jobs':'careers_url'}
    rows=[]
    for idx, row in enumerate(reader):
        if idx >= limit:
            raise ValueError(f'CSV limit exceeded ({limit} companies)')
        item = {}
        for key,value in row.items():
            if key is None: continue
            field = aliases.get(key.strip().casefold(), key.strip().casefold())
            item[field] = (value or '').strip()
        if not item.get('name'):
            continue
        rows.append(item)
    if not rows:
        raise ValueError('CSV contains no usable rows; include a name or company column')
    return rows
