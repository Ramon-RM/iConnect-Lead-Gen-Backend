"""
iConnect — Outbound API Routes
Campaign management, CSV upload, inbox registration, stats.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List, Optional
from uuid import UUID
from datetime import datetime, timezone
from pydantic import BaseModel

from app.db.database import get_db
from app.models.outbound_models import (
    Campaign, Prospect, SendingInbox, OutboundLog,
    CampaignStatus, SendingInboxStatus, ProspectStatus
)
from app.services.email.outbound_processor import import_prospects_from_csv

router = APIRouter(prefix="/api/outbound", tags=["outbound"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    client_id: UUID
    name: str
    niche: Optional[str] = None
    target_description: Optional[str] = None
    pain_point_focus: Optional[str] = None
    value_proposition: Optional[str] = None
    social_proof: Optional[str] = None
    cta: Optional[str] = "open to a 15-minute chat?"
    daily_send_limit: int = 30
    sequence_steps: int = 4
    step_delays_days: str = "0,3,7,14"


class CampaignResponse(BaseModel):
    id: UUID
    name: str
    niche: Optional[str]
    status: str
    total_prospects: int
    emails_sent: int
    replies_received: int
    reply_rate: float
    created_at: datetime

    class Config:
        from_attributes = True


class SendingInboxCreate(BaseModel):
    client_id: UUID
    email_address: str
    display_name: str
    domain: str
    daily_limit: int = 30


class SendingInboxResponse(BaseModel):
    id: UUID
    email_address: str
    display_name: str
    domain: str
    status: str
    warmup_day: int
    emails_sent_today: int
    daily_limit: int
    spam_rate: float

    class Config:
        from_attributes = True


class CampaignStats(BaseModel):
    total_prospects: int
    queued: int
    sent: int
    opened: int
    replied: int
    bounced: int
    reply_rate: float
    emails_sent_today: int


# ─── Campaigns ────────────────────────────────────────────────────────────────

@router.post("/campaigns", response_model=CampaignResponse)
async def create_campaign(payload: CampaignCreate, db: AsyncSession = Depends(get_db)):
    """Create a new outbound campaign."""
    campaign = Campaign(**payload.model_dump())
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.get("/campaigns", response_model=List[CampaignResponse])
async def list_campaigns(
    client_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Campaign)
        .where(Campaign.client_id == client_id)
        .order_by(Campaign.created_at.desc())
    )
    return result.scalars().all()


@router.get("/campaigns/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(campaign_id: UUID, db: AsyncSession = Depends(get_db)):
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return campaign


@router.patch("/campaigns/{campaign_id}/status")
async def update_campaign_status(
    campaign_id: UUID,
    status: str,
    db: AsyncSession = Depends(get_db),
):
    """Start, pause, or complete a campaign."""
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    valid = [s.value for s in CampaignStatus]
    if status not in valid:
        raise HTTPException(400, f"Invalid status. Choose from: {valid}")

    campaign.status = status
    if status == "active" and not campaign.started_at:
        campaign.started_at = datetime.now(timezone.utc)

    await db.commit()
    return {"campaign_id": campaign_id, "status": status}


@router.get("/campaigns/{campaign_id}/stats", response_model=CampaignStats)
async def campaign_stats(campaign_id: UUID, db: AsyncSession = Depends(get_db)):
    """Detailed stats for one campaign."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)

    total = await db.scalar(select(func.count(Prospect.id)).where(Prospect.campaign_id == campaign_id))
    queued = await db.scalar(select(func.count(Prospect.id)).where(Prospect.campaign_id == campaign_id, Prospect.status == "queued"))
    sent = await db.scalar(select(func.count(Prospect.id)).where(Prospect.campaign_id == campaign_id, Prospect.status == "sent"))
    opened = await db.scalar(select(func.count(Prospect.id)).where(Prospect.campaign_id == campaign_id, Prospect.status == "opened"))
    replied = await db.scalar(select(func.count(Prospect.id)).where(Prospect.campaign_id == campaign_id, Prospect.status == "replied"))
    bounced = await db.scalar(select(func.count(Prospect.id)).where(Prospect.campaign_id == campaign_id, Prospect.status == "bounced"))
    sent_today = await db.scalar(
        select(func.count(OutboundLog.id)).where(
            OutboundLog.campaign_id == campaign_id,
            OutboundLog.sent_at >= today_start
        )
    )

    campaign = await db.get(Campaign, campaign_id)

    return CampaignStats(
        total_prospects=total or 0,
        queued=queued or 0,
        sent=sent or 0,
        opened=opened or 0,
        replied=replied or 0,
        bounced=bounced or 0,
        reply_rate=campaign.reply_rate if campaign else 0.0,
        emails_sent_today=sent_today or 0,
    )


# ─── CSV Upload ───────────────────────────────────────────────────────────────

@router.post("/campaigns/{campaign_id}/prospects/upload")
async def upload_prospects_csv(
    campaign_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a CSV file of prospects to a campaign.
    Required columns: email, first_name, last_name, company, title
    Optional: linkedin_url, website, recent_post, custom_note
    """
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "File must be a .csv")

    content = await file.read()
    csv_text = content.decode("utf-8", errors="replace")

    result = await import_prospects_from_csv(
        db=db,
        campaign_id=campaign_id,
        client_id=campaign.client_id,
        csv_content=csv_text,
    )
    return result


@router.get("/campaigns/{campaign_id}/prospects")
async def list_prospects(
    campaign_id: UUID,
    status: Optional[str] = None,
    limit: int = Query(100, le=500),
    db: AsyncSession = Depends(get_db),
):
    query = select(Prospect).where(Prospect.campaign_id == campaign_id)
    if status:
        query = query.where(Prospect.status == status)
    query = query.order_by(Prospect.created_at.desc()).limit(limit)
    result = await db.execute(query)
    prospects = result.scalars().all()
    return [
        {
            "id": str(p.id),
            "email": p.email,
            "first_name": p.first_name,
            "company": p.company,
            "title": p.title,
            "status": p.status,
            "current_step": p.current_step,
            "next_email_at": p.next_email_at,
            "replied_at": p.replied_at,
        }
        for p in prospects
    ]


# ─── Sending Inboxes ──────────────────────────────────────────────────────────

@router.post("/inboxes", response_model=SendingInboxResponse)
async def register_sending_inbox(
    payload: SendingInboxCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Register a dedicated cold-email sending inbox.
    After registering, connect it via OAuth the same way as inbound.
    """
    inbox = SendingInbox(**payload.model_dump())
    db.add(inbox)
    await db.commit()
    await db.refresh(inbox)
    return inbox


@router.get("/inboxes", response_model=List[SendingInboxResponse])
async def list_inboxes(
    client_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SendingInbox)
        .where(SendingInbox.client_id == client_id)
        .order_by(SendingInbox.created_at.desc())
    )
    return result.scalars().all()


@router.patch("/inboxes/{inbox_id}/status")
async def update_inbox_status(
    inbox_id: UUID,
    status: str,
    db: AsyncSession = Depends(get_db),
):
    inbox = await db.get(SendingInbox, inbox_id)
    if not inbox:
        raise HTTPException(404, "Inbox not found")
    inbox.status = status
    await db.commit()
    return {"inbox_id": inbox_id, "status": status}