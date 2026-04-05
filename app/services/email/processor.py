"""
iConnect — Email Processor
The main orchestration layer.
Flow: new email → classify → generate reply → send → store → schedule follow-up
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.models.models import Lead, EmailLog, FollowUpQueue, ClientAccount, LeadStatus
from app.schemas.schemas import InboundEmailPayload, ClassificationResult
from app.services.ai.ai_service import classify_email, generate_reply, generate_followup
from app.services.email import gmail_service
from app.core.config import settings

logger = logging.getLogger(__name__)


# ─── Process Inbound Email ────────────────────────────────────────────────────

async def process_inbound_email(
    db: AsyncSession,
    client: ClientAccount,
    raw_email: dict,
) -> Optional[Lead]:
    """
    Full pipeline for one inbound email:
    1. Skip if already processed
    2. Classify with AI
    3. Skip if spam
    4. Create or update Lead
    5. Generate reply
    6. Send reply from client's account
    7. Log everything
    8. Schedule follow-ups
    """
    sender_email = raw_email["sender_email"]
    subject = raw_email["subject"]
    body = raw_email["body"]
    message_id = raw_email["message_id"]
    thread_id = raw_email["thread_id"]

    # ── 1. Skip if already processed ──────────────────────────────────────────
    existing_log = await db.execute(
        select(EmailLog).where(EmailLog.message_id == message_id)
    )
    if existing_log.scalar_one_or_none():
        logger.debug(f"Message {message_id} already processed, skipping")
        return None

    logger.info(f"Processing email from {sender_email} for client {client.id}")

    # ── 2. Classify ───────────────────────────────────────────────────────────
    classification, ai_provider, pt, ct = await classify_email(
        sender_email=sender_email,
        subject=subject,
        body=body,
        client_name=client.name,
        niche=client.niche or "business",
    )

    logger.info(f"Classified as: {classification.classification} | priority: {classification.reply_priority}")

    # ── 3. Skip spam ──────────────────────────────────────────────────────────
    if classification.classification == "spam":
        logger.info(f"Skipping spam email from {sender_email}")
        gmail_service.mark_as_read(client, message_id)
        return None

    # ── 4. Create or update Lead ──────────────────────────────────────────────
    lead = await _get_or_create_lead(
        db=db,
        client=client,
        sender_email=sender_email,
        sender_name=raw_email.get("sender_name"),
        subject=subject,
        body=body,
        thread_id=thread_id,
        classification=classification,
    )

    # If lead replied, cancel pending follow-ups
    await _cancel_pending_followups(db, lead.id)

    # ── 5. Get thread history for context ─────────────────────────────────────
    thread_history = await _get_thread_history(db, lead.id)

    # ── 6. Generate reply ─────────────────────────────────────────────────────
    reply_body, reply_provider, rpt, rct = await generate_reply(
        sender_email=sender_email,
        email_body=body,
        thread_history=thread_history,
        classification_result=classification,
        client_name=client.name,
        niche=client.niche or "business",
        tone_notes=client.tone_notes or "direct, warm, professional",
        proof_point=client.proof_point or "",
    )

# Iron out weird line breaks but keep paragraphs intact
    reply_body = "\n\n".join(paragraph.replace("\n", " ") for paragraph in reply_body.split("\n\n"))
    
    # ── 7. Send reply from client's Gmail ─────────────────────────────────────
    sent_message_id = None
    if client.email_provider == "gmail" and client.oauth_connected:
        try:
            sent_message_id = gmail_service.send_reply(
                client=client,
                to_email=sender_email,
                subject=subject,
                body=reply_body,
                thread_id=thread_id,
                in_reply_to=message_id,
            )
            gmail_service.mark_as_read(client, message_id)
        except Exception as e:
            logger.error(f"Failed to send reply for lead {lead.id}: {e}")

    # ── 8. Log inbound + outbound ─────────────────────────────────────────────
    inbound_log = EmailLog(
        client_id=client.id,
        lead_id=lead.id,
        direction="inbound",
        subject=subject,
        body=body,
        message_id=message_id,
        ai_generated=False,
    )
    db.add(inbound_log)

    outbound_log = EmailLog(
        client_id=client.id,
        lead_id=lead.id,
        direction="outbound",
        subject=f"Re: {subject}",
        body=reply_body,
        message_id=sent_message_id,
        ai_generated=True,
        ai_provider_used=reply_provider,
        ai_prompt_tokens=rpt,
        ai_completion_tokens=rct,
    )
    db.add(outbound_log)

    # Update lead status
    lead.status = LeadStatus.replied
    lead.last_contacted_at = datetime.now(timezone.utc)

    # ── 9. Schedule follow-ups (only for hot/warm) ─────────────────────────────
    if classification.classification in ("hot", "warm"):
        await _schedule_followups(db, lead, client)

    await db.commit()
    await db.refresh(lead)

    logger.info(f"Completed processing for lead {lead.id} | sent: {sent_message_id is not None}")
    return lead


# ─── Follow-up Processor ──────────────────────────────────────────────────────

async def process_due_followups(db: AsyncSession):
    """
    Called by Celery Beat every 5 minutes.
    Finds all due follow-ups and sends them.
    """
    now = datetime.now(timezone.utc)

    due = await db.execute(
        select(FollowUpQueue).where(
            and_(
                FollowUpQueue.is_sent == False,
                FollowUpQueue.is_cancelled == False,
                FollowUpQueue.scheduled_at <= now,
            )
        )
    )
    due_items = due.scalars().all()

    logger.info(f"Processing {len(due_items)} due follow-ups")

    for item in due_items:
        try:
            await _send_followup(db, item)
        except Exception as e:
            logger.error(f"Follow-up failed for queue item {item.id}: {e}")

    await db.commit()


async def _send_followup(db: AsyncSession, queue_item: FollowUpQueue):
    """Send one follow-up email."""
    lead = await db.get(Lead, queue_item.lead_id)
    client = await db.get(ClientAccount, queue_item.client_id)

    if not lead or not client:
        logger.warning(f"Lead or client not found for follow-up {queue_item.id}")
        return

    # Get message history
    thread_history = await _get_thread_history(db, lead.id)
    days_since = _days_since(lead.last_contacted_at)

    # Generate follow-up
    followup_body, provider, pt, ct = await generate_followup(
        lead_name=lead.sender_name or "",
        company=lead.company or "",
        original_intent=lead.intent or "their original inquiry",
        followup_number=queue_item.followup_number,
        days_since_contact=days_since,
        message_history=thread_history,
        client_name=client.name,
        niche=client.niche or "business",
        tone_notes=client.tone_notes or "direct, warm, professional",
    )

    # Send
    sent_id = None
    if client.email_provider == "gmail" and client.oauth_connected:
        sent_id = gmail_service.send_reply(
            client=client,
            to_email=lead.sender_email,
            subject=lead.subject or "Following up",
            body=followup_body,
            thread_id=lead.gmail_thread_id,
        )

    # Log
    log = EmailLog(
        client_id=client.id,
        lead_id=lead.id,
        direction="outbound",
        subject=f"Follow-up #{queue_item.followup_number}",
        body=followup_body,
        message_id=sent_id,
        ai_generated=True,
        ai_provider_used=provider,
        ai_prompt_tokens=pt,
        ai_completion_tokens=ct,
    )
    db.add(log)

    # Update queue item
    queue_item.is_sent = True
    queue_item.sent_at = datetime.now(timezone.utc)

    # Update lead
    lead.followup_count = (lead.followup_count or 0) + 1
    lead.last_contacted_at = datetime.now(timezone.utc)
    lead.status = getattr(LeadStatus, f"follow_up_{queue_item.followup_number}", lead.status)

    logger.info(f"Follow-up #{queue_item.followup_number} sent to {lead.sender_email}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_or_create_lead(
    db: AsyncSession,
    client: ClientAccount,
    sender_email: str,
    sender_name: Optional[str],
    subject: str,
    body: str,
    thread_id: str,
    classification: ClassificationResult,
) -> Lead:
    """Find existing lead by email or create a new one."""
    result = await db.execute(
        select(Lead).where(
            and_(Lead.client_id == client.id, Lead.sender_email == sender_email)
        )
    )
    lead = result.scalar_one_or_none()

    if lead:
        # Update classification if this is a new/better signal
        lead.classification = classification.classification
        lead.intent = classification.intent
        lead.urgency = classification.urgency
        lead.budget_signal = classification.budget_signal
        lead.reply_priority = classification.reply_priority
    else:
        lead = Lead(
            client_id=client.id,
            sender_email=sender_email,
            sender_name=sender_name,
            subject=subject,
            original_email_body=body,
            gmail_thread_id=thread_id,
            classification=classification.classification,
            intent=classification.intent,
            urgency=classification.urgency,
            budget_signal=classification.budget_signal,
            reply_priority=classification.reply_priority,
        )
        db.add(lead)
        await db.flush()  # get the ID

    return lead


async def _get_thread_history(db: AsyncSession, lead_id: UUID) -> str:
    """Build a plain-text thread history string for AI context."""
    result = await db.execute(
        select(EmailLog)
        .where(EmailLog.lead_id == lead_id)
        .order_by(EmailLog.sent_at.asc())
        .limit(10)
    )
    logs = result.scalars().all()

    if not logs:
        return ""

    lines = []
    for log in logs:
        direction = "THEM" if log.direction == "inbound" else "YOU"
        lines.append(f"[{direction}]: {log.body[:500]}")

    return "\n\n".join(lines)


async def _schedule_followups(
    db: AsyncSession,
    lead: Lead,
    client: ClientAccount,
):
    """Schedule 4 follow-ups at day 1, 3, 7, 14."""
    now = datetime.now(timezone.utc)
    schedule = [
        settings.followup_day_1,
        settings.followup_day_2,
        settings.followup_day_3,
        settings.followup_day_4,
    ]

    for i, days in enumerate(schedule, start=1):
        scheduled_at = now + timedelta(days=days)
        queue_item = FollowUpQueue(
            lead_id=lead.id,
            client_id=client.id,
            followup_number=i,
            scheduled_at=scheduled_at,
        )
        db.add(queue_item)

    lead.next_followup_at = now + timedelta(days=schedule[0])


async def _cancel_pending_followups(db: AsyncSession, lead_id: UUID):
    """Cancel all pending follow-ups when a lead replies."""
    result = await db.execute(
        select(FollowUpQueue).where(
            and_(
                FollowUpQueue.lead_id == lead_id,
                FollowUpQueue.is_sent == False,
                FollowUpQueue.is_cancelled == False,
            )
        )
    )
    items = result.scalars().all()
    for item in items:
        item.is_cancelled = True

    if items:
        logger.info(f"Cancelled {len(items)} follow-ups for lead {lead_id} (they replied)")


def _days_since(dt: Optional[datetime]) -> int:
    if not dt:
        return 0
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days