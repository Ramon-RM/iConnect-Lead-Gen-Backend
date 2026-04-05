"""
iConnect — Outbound Campaign Processor
Orchestrates the full outbound pipeline:
  CSV upload → validate → queue prospects → AI personalise → rotate inbox → send → track
"""
import csv
import io
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from app.models.outbound_models import (
    Campaign, Prospect, OutboundLog, SendingInbox,
    ProspectStatus, CampaignStatus
)
from app.models.models import ClientAccount, Lead
from app.services.ai.outbound_ai_service import (
    generate_cold_email, generate_cold_followup, generate_subject_line
)
from app.services.email.outbound_sender import get_available_inbox, send_cold_email

logger = logging.getLogger(__name__)

SKIP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "aol.com", "protonmail.com",
}


# ─── CSV Import ───────────────────────────────────────────────────────────────

async def import_prospects_from_csv(
    db: AsyncSession,
    campaign_id: UUID,
    client_id: UUID,
    csv_content: str,
) -> dict:
    """
    Parse a CSV and create Prospect rows.
    Expected columns: email, first_name, last_name, company, title
    Optional: linkedin_url, website, recent_post, custom_note

    Returns summary: {imported, skipped_duplicates, skipped_personal, errors}
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    imported = skipped_dup = skipped_personal = errors = 0

    for row in reader:
        email = (row.get("email") or "").strip().lower()
        if not email or "@" not in email:
            errors += 1
            continue

        # Skip personal email domains — only target business emails
        domain = email.split("@")[-1]
        #if domain in SKIP_DOMAINS:
         #   skipped_personal += 1
          #  continue

        # Skip duplicates within this campaign
        existing = await db.execute(
            select(Prospect).where(
                and_(Prospect.campaign_id == campaign_id, Prospect.email == email)
            )
        )
        if existing.scalar_one_or_none():
            skipped_dup += 1
            continue

        prospect = Prospect(
            campaign_id=campaign_id,
            client_id=client_id,
            email=email,
            first_name=(row.get("first_name") or "").strip(),
            last_name=(row.get("last_name") or "").strip(),
            company=(row.get("company") or "").strip(),
            title=(row.get("title") or "").strip(),
            linkedin_url=(row.get("linkedin_url") or "").strip(),
            website=(row.get("website") or "").strip(),
            recent_post=(row.get("recent_post") or "").strip(),
            company_news=(row.get("company_news") or "").strip(),
            custom_note=(row.get("custom_note") or "").strip(),
            status=ProspectStatus.queued,
            next_email_at=datetime.now(timezone.utc),
        )
        db.add(prospect)
        imported += 1

    # Update campaign total
    campaign = await db.get(Campaign, campaign_id)
    if campaign:
        campaign.total_prospects = (campaign.total_prospects or 0) + imported

    await db.commit()
    logger.info(f"CSV import for campaign {campaign_id}: {imported} imported, {skipped_dup} dupes, {skipped_personal} personal")

    return {
        "imported": imported,
        "skipped_duplicates": skipped_dup,
        "skipped_personal_emails": skipped_personal,
        "errors": errors,
    }


# ─── Campaign Send Loop ────────────────────────────────────────────────────────

async def process_campaign_sends(db: AsyncSession):
    """
    Called by Celery Beat every 30 minutes.
    Sends queued cold emails for all active campaigns.
    Respects daily inbox limits and campaign caps.
    """
    now = datetime.now(timezone.utc)

    # Find all active campaigns
    result = await db.execute(
        select(Campaign).where(Campaign.status == CampaignStatus.active)
    )
    campaigns = result.scalars().all()

    logger.info(f"Processing {len(campaigns)} active campaigns")

    for campaign in campaigns:
        await _process_one_campaign(db, campaign, now)

    await db.commit()


async def _process_one_campaign(db: AsyncSession, campaign: Campaign, now: datetime):
    """Send queued emails for one campaign, respecting daily limit."""
    client = await db.get(ClientAccount, campaign.client_id)
    if not client:
        return

    # Count emails already sent today for this campaign
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = await db.scalar(
        select(func.count(OutboundLog.id)).where(
            and_(
                OutboundLog.campaign_id == campaign.id,
                OutboundLog.sent_at >= today_start,
                OutboundLog.step_number == 1,  # only count initial emails
            )
        )
    )

    remaining_today = campaign.daily_send_limit - (sent_today or 0)
    if remaining_today <= 0:
        logger.info(f"Campaign {campaign.id} hit daily limit ({campaign.daily_send_limit})")
        return

    # Get prospects due to be emailed
    due = await db.execute(
        select(Prospect).where(
            and_(
                Prospect.campaign_id == campaign.id,
                Prospect.status.in_([ProspectStatus.queued, ProspectStatus.opened]),
                Prospect.next_email_at <= now,
            )
        ).limit(remaining_today)
    )
    prospects = due.scalars().all()

    logger.info(f"Campaign {campaign.id}: {len(prospects)} prospects due, {remaining_today} slots left today")

    for prospect in prospects:
        inbox = await get_available_inbox(db, campaign.client_id, campaign.daily_send_limit)
        if not inbox:
            logger.warning(f"No available inboxes for campaign {campaign.id} — stopping")
            break

        await _send_to_prospect(db, campaign, prospect, inbox, client)


async def _send_to_prospect(
    db: AsyncSession,
    campaign: Campaign,
    prospect: Prospect,
    inbox: SendingInbox,
    client: ClientAccount,
):
    """Generate and send one email to one prospect."""
    is_initial = prospect.current_step == 0

    try:
        if is_initial:
            # Initial cold email
            subject = await generate_subject_line(
                first_name=prospect.first_name or "there",
                company=prospect.company or "your company",
                pain_point_focus=campaign.pain_point_focus or "lead follow-up",
            )

            body, provider, pt, ct = await generate_cold_email(
                sender_name=inbox.display_name,
                company_name=client.name,
                value_proposition=campaign.value_proposition or "",
                pain_point_focus=campaign.pain_point_focus or "",
                social_proof=campaign.social_proof or "",
                cta=campaign.cta or "open to a 15-minute chat?",
                prospect_first_name=prospect.first_name or "there",
                prospect_company=prospect.company or "your business",
                prospect_title=prospect.title or "",
                personalisation_note=prospect.custom_note or prospect.recent_post or "",
            )

        else:
            # Follow-up email
            prev_logs = await _get_prospect_email_history(db, prospect.id)
            subject = f"Re: {campaign.email_subject or 'Following up'}"

            body, provider, pt, ct = await generate_cold_followup(
                sender_name=inbox.display_name,
                company_name=client.name,
                value_proposition=campaign.value_proposition or "",
                pain_point_focus=campaign.pain_point_focus or "",
                cta=campaign.cta or "worth a quick chat?",
                step_number=prospect.current_step + 1,
                prospect_first_name=prospect.first_name or "there",
                previous_emails_summary=prev_logs,
            )

        # Send via inbox rotation
        sent_id = await send_cold_email(
            inbox=inbox,
            to_email=prospect.email,
            subject=subject,
            body=body,
            db=db,
        )

        if not sent_id:
            logger.error(f"Send failed for prospect {prospect.id}")
            return

        # Log it
        log = OutboundLog(
            campaign_id=campaign.id,
            prospect_id=prospect.id,
            sending_inbox_id=inbox.id,
            step_number=prospect.current_step + 1,
            subject=subject,
            body=body,
            ai_provider_used=provider,
            ai_prompt_tokens=pt,
            ai_completion_tokens=ct,
            gmail_message_id=sent_id,
        )
        db.add(log)

        # Advance prospect
        prospect.current_step += 1
        prospect.last_emailed_at = datetime.now(timezone.utc)
        prospect.status = ProspectStatus.sent

        # Schedule next follow-up if within sequence
        step_delays = [int(d) for d in campaign.step_delays_days.split(",")]
        if prospect.current_step < len(step_delays):
            next_delay = step_delays[prospect.current_step]
            prospect.next_email_at = datetime.now(timezone.utc) + timedelta(days=next_delay)
        else:
            # Sequence exhausted
            prospect.status = ProspectStatus.do_not_contact
            prospect.next_email_at = None

        # Update campaign stats
        campaign.emails_sent = (campaign.emails_sent or 0) + 1

        logger.info(f"Cold email step {prospect.current_step} sent to {prospect.email}")

    except Exception as e:
        logger.error(f"Failed to send to prospect {prospect.id}: {e}")


# ─── Reply Detection ──────────────────────────────────────────────────────────

async def handle_prospect_reply(
    db: AsyncSession,
    campaign_id: UUID,
    prospect_email: str,
    reply_body: str,
    gmail_thread_id: Optional[str] = None,
) -> Optional[Lead]:
    """
    Called when a reply arrives to a cold email thread.
    1. Mark prospect as replied
    2. Cancel remaining follow-ups
    3. Convert to inbound Lead → hands off to inbound processor
    """
    result = await db.execute(
        select(Prospect).where(
            and_(Prospect.campaign_id == campaign_id, Prospect.email == prospect_email)
        )
    )
    prospect = result.scalar_one_or_none()
    if not prospect:
        return None

    prospect.status = ProspectStatus.replied
    prospect.replied_at = datetime.now(timezone.utc)
    prospect.next_email_at = None  # cancel all future follow-ups

    campaign = await db.get(Campaign, campaign_id)
    if campaign:
        campaign.replies_received = (campaign.replies_received or 0) + 1
        total = campaign.emails_sent or 1
        campaign.reply_rate = campaign.replies_received / total

    # Create inbound Lead from this prospect
    lead = Lead(
        client_id=prospect.client_id,
        sender_email=prospect.email,
        sender_name=f"{prospect.first_name or ''} {prospect.last_name or ''}".strip(),
        company=prospect.company,
        original_email_body=reply_body,
        gmail_thread_id=gmail_thread_id,
        subject=f"Re: campaign reply from {prospect.email}",
    )
    db.add(lead)
    await db.flush()

    prospect.converted_lead_id = lead.id
    await db.commit()

    logger.info(f"Prospect {prospect.email} replied — converted to lead {lead.id}")
    return lead


# ─── Helper ───────────────────────────────────────────────────────────────────

async def _get_prospect_email_history(db: AsyncSession, prospect_id: UUID) -> str:
    """Build a summary of previous emails sent to this prospect."""
    result = await db.execute(
        select(OutboundLog)
        .where(OutboundLog.prospect_id == prospect_id)
        .order_by(OutboundLog.sent_at.asc())
        .limit(4)
    )
    logs = result.scalars().all()
    if not logs:
        return "No previous emails."

    lines = [f"Email #{log.step_number}: {log.body[:300]}" for log in logs]
    return "\n\n".join(lines)