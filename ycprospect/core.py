"""Data fetching, directory filtering and conservative public-email extraction."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

DATA_URL = "https://yc-oss.github.io/api/companies/all.json"
USER_AGENT = "YCProspectKit/0.1 (public company research)"
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", re.I)
# Platform/operational addresses are not useful founder contacts.
EXCLUDED_EMAILS = {"noreply@ycombinator.com", "no-reply@ycombinator.com"}
EXCLUDED_LOCAL_PARTS = {"noreply", "no-reply", "postmaster", "webmaster", "abuse", "privacy", "legal"}
SUPPORTED = {
    "batch": "batch", "industry": "industry", "region": "region", "regions": "region",
    "status": "status", "stage": "stage", "tags": "tags", "team_size": "team_size",
    "isHiring": "isHiring", "top_company": "top_company", "nonprofit": "nonprofit",
    "app_video_public": "app_video_public", "demo_day_video_public": "demo_day_video_public",
    "app_answers": "app_answers", "question_answers": "question_answers",
    "q": "q", "query": "q",
}
BOOLEAN_KEYS = {"isHiring", "top_company", "nonprofit", "app_video_public", "demo_day_video_public", "app_answers", "question_answers"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_directory_url(url: str) -> tuple[dict, list[str]]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.ycombinator.com", "ycombinator.com"} or parsed.path.rstrip("/") != "/companies":
        raise ValueError("Use an HTTPS YC company-directory URL, e.g. https://www.ycombinator.com/companies?industry=B2B")
    raw = parse_qs(parsed.query, keep_blank_values=False)
    filters: dict[str, list[str]] = {}
    for key, values in raw.items():
        if key in SUPPORTED:
            filters.setdefault(SUPPORTED[key], []).extend(str(v).strip() for v in values if str(v).strip())
    ignored = sorted(set(raw) - set(SUPPORTED))
    return filters, ignored


def bool_value(raw: object) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        if raw.casefold() in {"true", "1", "yes"}:
            return True
        if raw.casefold() in {"false", "0", "no"}:
            return False
    return None


def as_strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def partial_match(actual: object, wanted: list[str]) -> bool:
    combined = " | ".join(as_strings(actual)).casefold()
    return any(val.casefold() in combined for val in wanted)


def team_size_matches(actual: object, filters: list[str]) -> bool:
    try:
        size = int(actual)
    except (ValueError, TypeError):
        return False
    for raw in filters:
        try:
            parsed = json.loads(raw) if raw.startswith("[") else raw
            if isinstance(parsed, list) and len(parsed) == 2:
                lower, upper = parsed
                low = int(str(lower).replace(",", "").replace("+", ""))
                high = float("inf") if "+" in str(upper) else int(str(upper).replace(",", ""))
                if low <= size <= high:
                    return True
            else:
                text = str(parsed).replace(",", "").strip()
                if "-" in text:
                    low, high = text.split("-", 1)
                    if int(low) <= size <= int(high):
                        return True
                elif text.endswith("+"):
                    if size >= int(text[:-1]):
                        return True
                elif size == int(text):
                    return True
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return False


def company_matches(company: dict, filters: dict[str, list[str]]) -> bool:
    fields: dict[str, object] = {
        "batch": company.get("batch"),
        "industry": [company.get("industry") or "", *as_strings(company.get("industries"))],
        "region": [*as_strings(company.get("regions")), company.get("all_locations") or ""],
        "status": company.get("status"),
        "stage": company.get("stage"),
        "tags": [*as_strings(company.get("tags")), *as_strings(company.get("tags_highlighted"))],
    }
    for key, actual in fields.items():
        if key in filters and not partial_match(actual, filters[key]):
            return False
    if "team_size" in filters and not team_size_matches(company.get("team_size"), filters["team_size"]):
        return False
    for key in BOOLEAN_KEYS & filters.keys():
        expected = [bool_value(value) for value in filters[key]]
        if None in expected:
            raise ValueError(f"Invalid boolean for {key}: {filters[key]}")
        if key == "app_answers":
            raw = company.get(key)
            actual = bool(raw) if not isinstance(raw, bool) else raw
        else:
            actual = company.get(key)
        if actual not in expected:
            return False
    if "q" in filters:
        searchable = " ".join(str(company.get(key) or "") for key in ("name", "one_liner", "long_description", "slug")).casefold()
        if not any(term.casefold() in searchable for term in filters["q"]):
            return False
    return True


def load_company_feed(session: requests.Session | None = None) -> list[dict]:
    client = session or requests.Session()
    response = client.get(DATA_URL, headers={"User-Agent": USER_AGENT}, timeout=(10, 45))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("YC OSS feed returned an unexpected format; expected a JSON list")
    return [company for company in data if isinstance(company, dict) and company.get("slug")]


def canonical_company_url(slug: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,119}", slug):
        raise ValueError("Invalid YC company slug")
    return f"https://www.ycombinator.com/companies/{slug}"


def extract_public_emails(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    mailto = []
    for tag in soup.select('a[href^="mailto:"]'):
        address = tag.get("href", "").split(":", 1)[-1].split("?", 1)[0]
        mailto.extend(EMAIL_RE.findall(address))
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    visible = soup.get_text(" ", strip=True)
    addresses = {e.casefold().strip(".,;:") for e in (*mailto, *EMAIL_RE.findall(visible))}
    return sorted(
        address for address in addresses
        if address not in EXCLUDED_EMAILS
        and not address.endswith("@ycombinator.com")
        and address.split("@", 1)[0] not in EXCLUDED_LOCAL_PARTS
        and len(address) <= 254
    )


def get_company_emails(slug: str, session: requests.Session | None = None) -> list[str]:
    client = session or requests.Session()
    response = client.get(canonical_company_url(slug), headers={"User-Agent": USER_AGENT}, timeout=(10, 20))
    response.raise_for_status()
    return extract_public_emails(response.text)
