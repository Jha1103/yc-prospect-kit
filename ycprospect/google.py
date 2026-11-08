"""Optional Google integrations. Never used without an explicit CLI command."""
from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path

from .db import company_rows

CREDENTIAL_FILE = "credentials.json"


def auth(scopes: list[str], token_file: str):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Google integration requires: pip install -r requirements-google.txt") from exc
    if not Path(CREDENTIAL_FILE).exists():
        raise RuntimeError("Missing credentials.json. Download Desktop OAuth credentials from Google Cloud; see README.")
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, scopes=scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIAL_FILE, scopes)
        creds = flow.run_local_server(port=0)
    Path(token_file).write_text(creds.to_json(), encoding="utf-8")
    return creds


def gmail_service():
    from googleapiclient.discovery import build
    creds = auth(["https://www.googleapis.com/auth/gmail.compose"], "gmail_token.json")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def mime_message(recipient: str, subject: str, body: str) -> dict:
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}


def create_gmail_draft(service, recipient: str, subject: str, body: str) -> str:
    result = service.users().drafts().create(userId="me", body={"message": mime_message(recipient, subject, body)}).execute()
    return result["id"]


def send_gmail_message(service, recipient: str, subject: str, body: str) -> str:
    result = service.users().messages().send(userId="me", body=mime_message(recipient, subject, body)).execute()
    return result["id"]


def export_sheet(db, spreadsheet_id: str, worksheet: str = "YC Prospect Export") -> int:
    """Explicit snapshot export: changes only the named tab, not other spreadsheet tabs."""
    from googleapiclient.discovery import build
    service = build("sheets", "v4", credentials=auth(["https://www.googleapis.com/auth/spreadsheets"], "sheets_token.json"), cache_discovery=False)
    api = service.spreadsheets()
    sheet = api.get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title))").execute()
    titles = {item["properties"]["title"] for item in sheet.get("sheets", [])}
    if worksheet not in titles:
        api.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{"addSheet": {"properties": {"title": worksheet}}}]}).execute()
    columns = ["slug", "name", "batch", "industry", "location", "website", "description", "status", "team_size", "yc_url", "last_scanned", "scan_error", "public_emails"]
    values = [columns]
    for row in company_rows(db):
        values.append([str(row[col] if row[col] is not None else "") for col in columns])
    # Replaces only the dedicated export worksheet; SQLite remains authoritative.
    range_ = "'" + worksheet.replace("'", "''") + "'"
    api.values().clear(spreadsheetId=spreadsheet_id, range=range_, body={}).execute()
    api.values().update(spreadsheetId=spreadsheet_id, range=range_ + "!A1", valueInputOption="RAW", body={"values": values}).execute()
    return len(values) - 1
