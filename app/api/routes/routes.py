"""
iConnect — API Routes
All HTTP endpoints.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List, Optional
from uuid import UUID
from datetime import datetime, timezone

from app.db.database import get_db
from app.models.models import ClientAccount, Lead, EmailLog, FollowUpQueue, LeadClassification
from app.schemas.schemas import (
    ClientCreate, ClientUpdate, ClientResponse,
    LeadResponse, LeadUpdate,
    ReplyRequest, ReplyResponse,
    OAuthConnectResponse, PipelineStats,
    EmailLogResponse,
)
from app.services.email import gmail_service
from app.services.ai.ai_service import generate_reply as ai_generate_reply
from app.services.email.processor import _get_thread_history

router = APIRouter()


# ─── Client Accounts ─────────────────────────────────────────────────────────

@router.post("/clients", response_model=ClientResponse, tags=["clients"])
async def create_client(payload: ClientCreate, db: AsyncSession = Depends(get_db)):
    """Register a new client account."""
    client = ClientAccount(**payload.model_dump())
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return client


@router.get("/clients", response_model=List[ClientResponse], tags=["clients"])
async def list_clients(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ClientAccount).order_by(ClientAccount.created_at.desc()))
    return result.scalars().all()


@router.get("/clients/{client_id}", response_model=ClientResponse, tags=["clients"])
async def get_client(client_id: UUID, db: AsyncSession = Depends(get_db)):
    client = await db.get(ClientAccount, client_id)
    if not client:
        raise HTTPException(404, "Client not found")
    return client


@router.patch("/clients/{client_id}", response_model=ClientResponse, tags=["clients"])
async def update_client(client_id: UUID, payload: ClientUpdate, db: AsyncSession = Depends(get_db)):
    client = await db.get(ClientAccount, client_id)
    if not client:
        raise HTTPException(404, "Client not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(client, field, value)
    await db.commit()
    await db.refresh(client)
    return client


# ─── Gmail OAuth ─────────────────────────────────────────────────────────────

@router.get("/auth/gmail/connect/{client_id}", tags=["auth"])
async def gmail_connect(client_id: str):
    """Step 1: Return the Google OAuth URL for the client to visit."""
    auth_url = gmail_service.get_auth_url(state=client_id)
    return {"auth_url": auth_url}


@router.get("/auth/gmail/callback", response_model=OAuthConnectResponse, tags=["auth"])
async def gmail_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Step 2: Google redirects here with the code. Exchange for tokens."""
    client = await db.get(ClientAccount, UUID(state))
    if not client:
        raise HTTPException(404, "Client not found")

    tokens = gmail_service.exchange_code_for_tokens(code)
    email_address = gmail_service.get_user_email(tokens["access_token"])

    client.oauth_access_token = tokens["access_token"]
    client.oauth_refresh_token = tokens["refresh_token"]
    client.oauth_token_expiry = tokens["token_expiry"]
    client.email_provider = "gmail"
    client.email_address = email_address
    client.oauth_connected = True

    await db.commit()

    return OAuthConnectResponse(
        client_id=client.id,
        email_address=email_address,
        provider="gmail",
        connected=True,
    )


# ─── Leads ────────────────────────────────────────────────────────────────────

@router.get("/clients/{client_id}/leads", response_model=List[LeadResponse], tags=["leads"])
async def list_leads(
    client_id: UUID,
    status: Optional[str] = None,
    classification: Optional[str] = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    query = select(Lead).where(Lead.client_id == client_id)
    if status:
        query = query.where(Lead.status == status)
    if classification:
        query = query.where(Lead.classification == classification)
    query = query.order_by(Lead.reply_priority.desc(), Lead.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/leads/{lead_id}", response_model=LeadResponse, tags=["leads"])
async def get_lead(lead_id: UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


@router.patch("/leads/{lead_id}", response_model=LeadResponse, tags=["leads"])
async def update_lead(lead_id: UUID, payload: LeadUpdate, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(lead, field, value)
    await db.commit()
    await db.refresh(lead)
    return lead


@router.get("/leads/{lead_id}/emails", response_model=List[EmailLogResponse], tags=["leads"])
async def get_lead_emails(lead_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(EmailLog)
        .where(EmailLog.lead_id == lead_id)
        .order_by(EmailLog.sent_at.asc())
    )
    return result.scalars().all()


# ─── Manual Reply ─────────────────────────────────────────────────────────────

@router.post("/leads/{lead_id}/reply", response_model=ReplyResponse, tags=["leads"])
async def manual_reply(lead_id: UUID, payload: ReplyRequest, db: AsyncSession = Depends(get_db)):
    """
    Manually trigger an AI reply for a lead from the dashboard.
    Useful for hot leads you want to review before sending.
    """
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    client = await db.get(ClientAccount, lead.client_id)
    thread_history = await _get_thread_history(db, lead_id)

    from app.schemas.schemas import ClassificationResult
    classification = ClassificationResult(
        classification=lead.classification or "warm",
        intent=lead.intent or "general inquiry",
        urgency=lead.urgency or "medium",
        budget_signal=lead.budget_signal or "none",
        reply_priority=lead.reply_priority or 3,
    )

    reply_body, provider, _, _ = await ai_generate_reply(
        sender_email=lead.sender_email,
        email_body=lead.original_email_body or "",
        thread_history=thread_history,
        classification_result=classification,
        client_name=client.name,
        niche=client.niche or "business",
        tone_notes=client.tone_notes or "direct, warm, professional",
        proof_point=client.proof_point or "",
    )

    return ReplyResponse(
        lead_id=lead_id,
        reply_body=reply_body,
        ai_provider_used=provider,
        sent=False,  # preview only — dashboard can confirm send
    )


# ─── Pipeline Stats ───────────────────────────────────────────────────────────

@router.get("/clients/{client_id}/stats", response_model=PipelineStats, tags=["dashboard"])
async def pipeline_stats(client_id: UUID, db: AsyncSession = Depends(get_db)):
    """Summary stats for the client dashboard."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)

    total = await db.scalar(select(func.count(Lead.id)).where(Lead.client_id == client_id))
    hot = await db.scalar(select(func.count(Lead.id)).where(and_(Lead.client_id == client_id, Lead.classification == "hot")))
    warm = await db.scalar(select(func.count(Lead.id)).where(and_(Lead.client_id == client_id, Lead.classification == "warm")))
    cold = await db.scalar(select(func.count(Lead.id)).where(and_(Lead.client_id == client_id, Lead.classification == "cold")))
    replied = await db.scalar(select(func.count(Lead.id)).where(and_(Lead.client_id == client_id, Lead.status == "replied")))
    booked = await db.scalar(select(func.count(Lead.id)).where(and_(Lead.client_id == client_id, Lead.status == "booked")))
    won = await db.scalar(select(func.count(Lead.id)).where(and_(Lead.client_id == client_id, Lead.status == "closed_won")))
    emails_today = await db.scalar(
        select(func.count(EmailLog.id)).where(
            and_(EmailLog.client_id == client_id, EmailLog.direction == "outbound", EmailLog.sent_at >= today_start)
        )
    )
    pending_followups = await db.scalar(
        select(func.count(FollowUpQueue.id)).where(
            and_(FollowUpQueue.client_id == client_id, FollowUpQueue.is_sent == False, FollowUpQueue.is_cancelled == False)
        )
    )

    return PipelineStats(
        total_leads=total or 0,
        hot=hot or 0,
        warm=warm or 0,
        cold=cold or 0,
        replied=replied or 0,
        booked=booked or 0,
        closed_won=won or 0,
        emails_sent_today=emails_today or 0,
        followups_pending=pending_followups or 0,
    )