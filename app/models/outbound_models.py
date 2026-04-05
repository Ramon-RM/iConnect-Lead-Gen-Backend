"""
iConnect — Outbound Models
New tables that power cold outreach campaigns.
Sits alongside existing inbound models.
"""
from sqlalchemy import Column, String, Text, Integer, Boolean, DateTime, Float, ForeignKey, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.models.models import Base
import uuid
import enum


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    completed = "completed"


class ProspectStatus(str, enum.Enum):
    queued = "queued"
    sent = "sent"
    opened = "opened"
    replied = "replied"         # handed to inbound processor
    bounced = "bounced"
    unsubscribed = "unsubscribed"
    do_not_contact = "do_not_contact"


class SendingInboxStatus(str, enum.Enum):
    warming = "warming"         # new inbox, ramping up
    active = "active"
    paused = "paused"
    flagged = "flagged"         # spam rate too high


# ─── Sending Inbox (inbox rotation) ──────────────────────────────────────────

class SendingInbox(Base):
    """
    A dedicated sending email account used ONLY for cold outbound.
    NEVER use the client's main Gmail for cold outreach.

    Each inbox sends max 30-40 emails/day.
    Multiple inboxes rotate to reach safe daily volume.

    Example: 10 inboxes × 35 emails = 350 safe cold emails/day
    """
    __tablename__ = "sending_inboxes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=False)

    email_address = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255), nullable=False)  # "Alex from iConnect"
    domain = Column(String(255), nullable=False)         # tryiconnect.com

    # OAuth tokens (same pattern as main inbox)
    oauth_access_token = Column(Text, nullable=True)
    oauth_refresh_token = Column(Text, nullable=True)
    oauth_token_expiry = Column(DateTime(timezone=True), nullable=True)

    # Safety limits
    daily_limit = Column(Integer, default=30)            # hard cap per day
    emails_sent_today = Column(Integer, default=0)
    last_reset_date = Column(DateTime(timezone=True), nullable=True)

    # Warmup tracking
    status = Column(Enum(SendingInboxStatus), default=SendingInboxStatus.warming)
    warmup_day = Column(Integer, default=1)              # day in warmup sequence
    spam_rate = Column(Float, default=0.0)               # % bounced/spam reports

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    campaigns = relationship("Campaign", back_populates="sending_inbox")


# ─── Campaign ─────────────────────────────────────────────────────────────────

class Campaign(Base):
    """
    One outbound campaign = one niche, one sequence, one set of prospects.
    A client can run multiple campaigns simultaneously.
    """
    __tablename__ = "campaigns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=False)
    sending_inbox_id = Column(UUID(as_uuid=True), ForeignKey("sending_inboxes.id"), nullable=True)

    name = Column(String(255), nullable=False)           # "US Facebook Ads Agencies - Apr"
    niche = Column(String(100), nullable=True)
    target_description = Column(Text, nullable=True)     # "Agency owners, 2-20 employees, US"

    # Email sequence settings
    email_subject = Column(Text, nullable=True)          # AI generates per lead if null
    sequence_steps = Column(Integer, default=4)          # how many follow-ups
    step_delays_days = Column(String(50), default="0,3,7,14")  # comma-separated

    # AI personalisation context
    pain_point_focus = Column(Text, nullable=True)       # "losing leads to slow follow-up"
    value_proposition = Column(Text, nullable=True)      # "auto-reply in 60 seconds"
    social_proof = Column(Text, nullable=True)           # "helped X agency get 20 leads/mo"
    cta = Column(Text, nullable=True)                    # "open to a 15-min chat?"

    # Safety: daily send cap for this campaign
    daily_send_limit = Column(Integer, default=30)
    total_prospects = Column(Integer, default=0)
    emails_sent = Column(Integer, default=0)
    replies_received = Column(Integer, default=0)
    reply_rate = Column(Float, default=0.0)

    status = Column(Enum(CampaignStatus), default=CampaignStatus.draft)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sending_inbox = relationship("SendingInbox", back_populates="campaigns")
    prospects = relationship("Prospect", back_populates="campaign", cascade="all, delete-orphan")
    outbound_logs = relationship("OutboundLog", back_populates="campaign")


# ─── Prospect ─────────────────────────────────────────────────────────────────

class Prospect(Base):
    """
    One person in one campaign.
    Distinct from Lead (which is inbound).
    If a prospect replies → becomes a Lead in the inbound system.
    """
    __tablename__ = "prospects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=False)

    # Contact info
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    email = Column(String(255), nullable=False)
    company = Column(String(255), nullable=True)
    title = Column(String(255), nullable=True)
    linkedin_url = Column(String(500), nullable=True)
    website = Column(String(500), nullable=True)

    # Personalisation data (from Apollo / manual research)
    recent_post = Column(Text, nullable=True)           # for hyperpersonalisation
    company_news = Column(Text, nullable=True)          # "just raised Series A"
    custom_note = Column(Text, nullable=True)           # manual research note

    # Status
    status = Column(Enum(ProspectStatus), default=ProspectStatus.queued)
    current_step = Column(Integer, default=0)           # which email in sequence
    next_email_at = Column(DateTime(timezone=True), nullable=True)
    last_emailed_at = Column(DateTime(timezone=True), nullable=True)
    replied_at = Column(DateTime(timezone=True), nullable=True)

    # Converted to inbound lead?
    converted_lead_id = Column(UUID(as_uuid=True), ForeignKey("leads.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    campaign = relationship("Campaign", back_populates="prospects")
    outbound_logs = relationship("OutboundLog", back_populates="prospect")


# ─── Outbound Log ─────────────────────────────────────────────────────────────

class OutboundLog(Base):
    """Every cold email sent, with full AI-generated body stored."""
    __tablename__ = "outbound_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False)
    prospect_id = Column(UUID(as_uuid=True), ForeignKey("prospects.id"), nullable=False)
    sending_inbox_id = Column(UUID(as_uuid=True), ForeignKey("sending_inboxes.id"), nullable=True)

    step_number = Column(Integer, nullable=False)       # 1 = initial, 2+ = follow-ups
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    # Events
    opened_at = Column(DateTime(timezone=True), nullable=True)
    replied_at = Column(DateTime(timezone=True), nullable=True)
    bounced_at = Column(DateTime(timezone=True), nullable=True)
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)

    # AI metadata
    ai_provider_used = Column(String(20), nullable=True)
    ai_prompt_tokens = Column(Integer, nullable=True)
    ai_completion_tokens = Column(Integer, nullable=True)

    # Gmail message ID for threading
    gmail_message_id = Column(String(255), nullable=True)
    gmail_thread_id = Column(String(255), nullable=True)

    campaign = relationship("Campaign", back_populates="outbound_logs")
    prospect = relationship("Prospect", back_populates="outbound_logs")