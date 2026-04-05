"""
iConnect — Outbound Sender
Handles safe cold email sending with inbox rotation.

CRITICAL SAFETY RULES:
  1. Never send from a client's main connected inbox (ClientAccount.email_address)
  2. Always use dedicated SendingInbox accounts
  3. Never exceed daily_limit per inbox (default: 30)
  4. Reset daily counter at midnight UTC
  5. Pause inbox if spam_rate > 3%
"""
import logging
from datetime import datetime, timezone, date
from typing import Optional
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.models.outbound_models import SendingInbox, SendingInboxStatus
from app.services.email.gmail_service import _build_credentials, _build_service
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

MAX_SPAM_RATE = 0.03            # 3% — pause inbox if exceeded
WARMUP_SCHEDULE = [5, 10, 15, 20, 25, 30]  # emails per day for warmup days 1-6+


def get_warmup_limit(warmup_day: int) -> int:
    """Return safe daily limit based on inbox age."""
    idx = min(warmup_day - 1, len(WARMUP_SCHEDULE) - 1)
    return WARMUP_SCHEDULE[idx]


async def get_available_inbox(
    db: AsyncSession,
    client_id,
    campaign_daily_limit: int = 30,
) -> Optional[SendingInbox]:
    """
    Find an active sending inbox with remaining daily capacity.
    Rotates across inboxes so no single one is overloaded.
    """
    today = date.today()

    result = await db.execute(
        select(SendingInbox).where(
            and_(
                SendingInbox.client_id == client_id,
                SendingInbox.status == SendingInboxStatus.active,
            )
        ).order_by(SendingInbox.emails_sent_today.asc())  # pick least-used first
    )
    inboxes = result.scalars().all()

    for inbox in inboxes:
        # Reset daily counter if it's a new day
        if inbox.last_reset_date and inbox.last_reset_date.date() < today:
            inbox.emails_sent_today = 0
            inbox.last_reset_date = datetime.now(timezone.utc)

        effective_limit = min(
            inbox.daily_limit,
            get_warmup_limit(inbox.warmup_day),
            campaign_daily_limit,
        )

        if inbox.emails_sent_today < effective_limit:
            return inbox

    return None  # all inboxes at capacity for today


async def send_cold_email(
    inbox: SendingInbox,
    to_email: str,
    subject: str,
    body: str,
    db: AsyncSession,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> Optional[str]:
    """
    Send a cold email from a dedicated sending inbox.
    Returns the sent message ID, or None on failure.
    """
    if inbox.status != SendingInboxStatus.active:
        logger.warning(f"Inbox {inbox.email_address} is not active — skipping")
        return None

    try:
        # Build service using inbox credentials (same pattern as inbound)
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from app.core.config import settings

        SCOPES = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ]

        creds = Credentials(
            token=inbox.oauth_access_token,
            refresh_token=inbox.oauth_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        msg = MIMEMultipart("alternative")
        msg["To"] = to_email
        msg["From"] = f"{inbox.display_name} <{inbox.email_address}>"
        msg["Subject"] = subject

        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        # Convert to HTML to force perfect, professional formatting
        html_body = body.replace("\r", "") # Strip invisible Windows characters
        html_body = html_body.replace("\n\n", "<br><br>") # Keep true paragraphs
        html_body = html_body.replace("\n", " ") # Squash mid-sentence breaks

        # Wrap in a clean, modern font
        final_html = f"<html><body style='font-family: Arial, sans-serif; font-size: 14px; color: #333;'>{html_body}</body></html>"
        msg.attach(MIMEText(final_html, "html"))
        
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        sent = service.users().messages().send(userId="me", body=payload).execute()

        # Increment daily counter
        inbox.emails_sent_today = (inbox.emails_sent_today or 0) + 1
        inbox.last_reset_date = datetime.now(timezone.utc)

        logger.info(f"Cold email sent via {inbox.email_address} to {to_email}")
        return sent["id"]

    except HttpError as e:
        logger.error(f"Cold email send failed from {inbox.email_address} to {to_email}: {e}")
        return None


async def update_inbox_warmup(db: AsyncSession, inbox: SendingInbox):
    """
    Advance warmup day once per calendar day.
    Called by the daily scheduler.
    """
    today = date.today()
    if inbox.last_reset_date and inbox.last_reset_date.date() < today:
        inbox.warmup_day = min(inbox.warmup_day + 1, len(WARMUP_SCHEDULE))
        if inbox.warmup_day >= len(WARMUP_SCHEDULE):
            inbox.status = SendingInboxStatus.active
        logger.info(f"Inbox {inbox.email_address} warmup day: {inbox.warmup_day}")


async def check_inbox_health(inbox: SendingInbox, db: AsyncSession):
    """Pause inbox if spam rate exceeded."""
    if inbox.spam_rate and inbox.spam_rate > MAX_SPAM_RATE:
        inbox.status = SendingInboxStatus.flagged
        logger.warning(f"Inbox {inbox.email_address} flagged — spam rate {inbox.spam_rate:.1%}")
        await db.commit()