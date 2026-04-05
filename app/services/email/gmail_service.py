"""
iConnect — Gmail Service
OAuth 2.0 connect, fetch new emails, send replies from client's account.
"""
import logging
import base64
import email as email_lib
from datetime import datetime, timezone
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.core.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


# ─── OAuth Flow ───────────────────────────────────────────────────────────────

def get_oauth_flow() -> Flow:
    """Build the Google OAuth flow object."""
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = settings.google_redirect_uri
    return flow


def get_auth_url(state: str) -> str:
    """Get the Google authorization URL. State = client_id for callback lookup."""
    flow = get_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange OAuth code for access + refresh tokens."""
    flow = get_oauth_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_expiry": creds.expiry,
    }


def get_user_email(access_token: str) -> str:
    """Get the Gmail address for the connected account."""
    import httpx
    resp = httpx.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return resp.json().get("email", "")


# ─── Credentials Helper ───────────────────────────────────────────────────────

def _build_credentials(client) -> Credentials:
    """Build refreshable Credentials from a ClientAccount ORM object."""
    creds = Credentials(
        token=client.oauth_access_token,
        refresh_token=client.oauth_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def _build_service(client):
    """Build an authorized Gmail API service for a client."""
    creds = _build_credentials(client)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ─── Fetch New Emails ─────────────────────────────────────────────────────────

def fetch_new_emails(client, since_timestamp: Optional[datetime] = None) -> list[dict]:
    """
    Fetch unread emails from the client's inbox.
    Returns a list of parsed email dicts.
    """
    service = _build_service(client)

    # Build query: unread, not spam, after timestamp
    query = "is:unread -category:spam -category:promotions"
    if since_timestamp:
        ts = int(since_timestamp.timestamp())
        query += f" after:{ts}"

    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()

        messages = results.get("messages", [])
        parsed = []

        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me",
                id=msg_ref["id"],
                format="full"
            ).execute()
            parsed_msg = _parse_gmail_message(msg)
            if parsed_msg:
                parsed.append(parsed_msg)

        return parsed

    except HttpError as e:
        logger.error(f"Gmail fetch error for client {client.id}: {e}")
        return []


def _parse_gmail_message(msg: dict) -> Optional[dict]:
    """Extract relevant fields from a raw Gmail message."""
    try:
        headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}

        # Extract body
        body = _extract_body(msg["payload"])

        return {
            "message_id": msg["id"],
            "thread_id": msg["threadId"],
            "subject": headers.get("subject", "(no subject)"),
            "sender_raw": headers.get("from", ""),
            "sender_email": _parse_email_address(headers.get("from", "")),
            "sender_name": _parse_sender_name(headers.get("from", "")),
            "body": body,
            "date": headers.get("date", ""),
        }
    except Exception as e:
        logger.error(f"Error parsing Gmail message: {e}")
        return None


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text body from Gmail payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if "parts" in payload:
        for part in payload["parts"]:
            body = _extract_body(part)
            if body:
                return body

    return ""


def _parse_email_address(from_header: str) -> str:
    """Extract email from 'Name <email@domain.com>'."""
    if "<" in from_header:
        return from_header.split("<")[1].rstrip(">").strip()
    return from_header.strip()


def _parse_sender_name(from_header: str) -> str:
    """Extract name from 'Name <email@domain.com>'."""
    if "<" in from_header:
        return from_header.split("<")[0].strip().strip('"')
    return ""


# ─── Send Email ───────────────────────────────────────────────────────────────

def send_reply(
    client,
    to_email: str,
    subject: str,
    body: str,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> str:
    """
    Send an email FROM the client's Gmail account.
    Returns the sent message ID.
    """
    service = _build_service(client)

    msg = MIMEMultipart("alternative")
    msg["To"] = to_email
    msg["From"] = client.email_address
    msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body_payload = {"raw": raw}

    if thread_id:
        body_payload["threadId"] = thread_id

    try:
        sent = service.users().messages().send(
            userId="me", body=body_payload
        ).execute()
        logger.info(f"Email sent for client {client.id} to {to_email} | msg_id: {sent['id']}")
        return sent["id"]
    except HttpError as e:
        logger.error(f"Gmail send error for client {client.id}: {e}")
        raise


def mark_as_read(client, message_id: str):
    """Mark a message as read after processing."""
    service = _build_service(client)
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except HttpError as e:
        logger.warning(f"Could not mark message {message_id} as read: {e}")