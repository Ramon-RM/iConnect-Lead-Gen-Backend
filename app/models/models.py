"""
iConnect — Database Models
All tables live in Supabase (PostgreSQL).
"""
from sqlalchemy import (
    Column, String, Text, Integer, Boolean,
    DateTime, Enum, ForeignKey, JSON
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func
import uuid
import enum

Base = declarative_base()


# ─── Enums ────────────────────────────────────────────────────────────────────

class EmailProvider(str, enum.Enum):
    gmail = "gmail"
    outlook = "outlook"


class LeadClassification(str, enum.Enum):
    hot = "hot"
    warm = "warm"
    cold = "cold"
    spam = "spam"
    existing_client = "existing_client"


class LeadStatus(str, enum.Enum):
    new = "new"
    replied = "replied"
    follow_up_1 = "follow_up_1"
    follow_up_2 = "follow_up_2"
    follow_up_3 = "follow_up_3"
    follow_up_4 = "follow_up_4"
    booked = "booked"
    closed_won = "closed_won"
    closed_lost = "closed_lost"
    unsubscribed = "unsubscribed"


class Urgency(str, enum.Enum):
    high = "high"
    medium = "medium"
    low = "low"


class AIProvider(str, enum.Enum):
    claude = "claude"
    groq = "groq"


# ─── Client Account ───────────────────────────────────────────────────────────

class ClientAccount(Base):
    """
    A business that has signed up for iConnect.
    Each client connects their own email (Gmail or Outlook).
    """
    __tablename__ = "client_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    niche = Column(String(100), nullable=True)          # e.g. "facebook_ads_agency"
    tone_notes = Column(Text, nullable=True)             # e.g. "direct, warm, no fluff"
    proof_point = Column(Text, nullable=True)            # their best case study / result

    # Email OAuth
    email_provider = Column(Enum(EmailProvider), nullable=True)
    email_address = Column(String(255), nullable=True)  # the FROM address
    oauth_access_token = Column(Text, nullable=True)
    oauth_refresh_token = Column(Text, nullable=True)
    oauth_token_expiry = Column(DateTime(timezone=True), nullable=True)
    oauth_connected = Column(Boolean, default=False)

    # AI provider for this client
    ai_provider = Column(Enum(AIProvider), default=AIProvider.claude)

    # Meta
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    leads = relationship("Lead", back_populates="client", cascade="all, delete-orphan")
    email_logs = relationship("EmailLog", back_populates="client")


# ─── Lead ─────────────────────────────────────────────────────────────────────

class Lead(Base):
    """
    A person who emailed the client.
    One lead = one conversation thread.
    """
    __tablename__ = "leads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=False)

    # Contact info
    sender_name = Column(String(255), nullable=True)
    sender_email = Column(String(255), nullable=False)
    company = Column(String(255), nullable=True)

    # Classification (set by AI)
    classification = Column(Enum(LeadClassification), nullable=True)
    intent = Column(Text, nullable=True)                # one-sentence summary
    urgency = Column(Enum(Urgency), nullable=True)
    budget_signal = Column(Text, nullable=True)
    reply_priority = Column(Integer, nullable=True)     # 1–5

    # Status tracking
    status = Column(Enum(LeadStatus), default=LeadStatus.new)
    followup_count = Column(Integer, default=0)
    last_contacted_at = Column(DateTime(timezone=True), nullable=True)
    next_followup_at = Column(DateTime(timezone=True), nullable=True)

    # Thread
    gmail_thread_id = Column(String(255), nullable=True)
    outlook_conversation_id = Column(String(255), nullable=True)
    subject = Column(Text, nullable=True)
    original_email_body = Column(Text, nullable=True)

    # Meta
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    client = relationship("ClientAccount", back_populates="leads")
    email_logs = relationship("EmailLog", back_populates="lead", cascade="all, delete-orphan")


# ─── Email Log ────────────────────────────────────────────────────────────────

class EmailLog(Base):
    """
    Every email sent or received, with the AI reply stored.
    Full thread history for context on next reply.
    """
    __tablename__ = "email_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=False)
    lead_id = Column(UUID(as_uuid=True), ForeignKey("leads.id"), nullable=False)

    direction = Column(String(10), nullable=False)      # "inbound" | "outbound"
    subject = Column(Text, nullable=True)
    body = Column(Text, nullable=False)
    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    # AI metadata
    ai_generated = Column(Boolean, default=False)
    ai_provider_used = Column(Enum(AIProvider), nullable=True)
    ai_prompt_tokens = Column(Integer, nullable=True)
    ai_completion_tokens = Column(Integer, nullable=True)

    # Gmail / Outlook message ID for threading
    message_id = Column(String(255), nullable=True)

    # Relationships
    client = relationship("ClientAccount", back_populates="email_logs")
    lead = relationship("Lead", back_populates="email_logs")


# ─── Follow-up Queue ──────────────────────────────────────────────────────────

class FollowUpQueue(Base):
    """
    Scheduled follow-ups. Celery Beat checks this table
    and fires emails when scheduled_at <= now().
    """
    __tablename__ = "followup_queue"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id = Column(UUID(as_uuid=True), ForeignKey("leads.id"), nullable=False)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id"), nullable=False)

    followup_number = Column(Integer, nullable=False)   # 1, 2, 3, or 4
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    is_sent = Column(Boolean, default=False)
    is_cancelled = Column(Boolean, default=False)       # cancelled if lead replies

    created_at = Column(DateTime(timezone=True), server_default=func.now())