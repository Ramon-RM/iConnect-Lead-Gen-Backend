from pydantic import BaseModel
from typing import Optional, List
from uuid import UUID
from datetime import datetime

class ClassificationResult(BaseModel):
    classification: str
    intent: str
    urgency: str
    budget_signal: str
    reply_priority: int

class InboundEmailPayload(BaseModel):
    sender_email: str
    subject: str
    body: str
    message_id: str
    thread_id: str
    sender_name: Optional[str] = None

class ClientCreate(BaseModel):
    name: str
    email: str

class ClientUpdate(BaseModel):
    name: Optional[str] = None
    niche: Optional[str] = None
    tone_notes: Optional[str] = None
    proof_point: Optional[str] = None

class ClientResponse(ClientCreate):
    id: UUID
    niche: Optional[str]
    tone_notes: Optional[str]
    proof_point: Optional[str]
    oauth_connected: bool
    class Config:
        from_attributes = True

class LeadUpdate(BaseModel):
    status: str

class LeadResponse(BaseModel):
    id: UUID
    sender_email: str
    sender_name: Optional[str]
    classification: Optional[str]
    intent: Optional[str]
    status: str
    class Config:
        from_attributes = True

class ReplyRequest(BaseModel):
    pass 

class ReplyResponse(BaseModel):
    lead_id: UUID
    reply_body: str
    ai_provider_used: str
    sent: bool

class OAuthConnectResponse(BaseModel):
    client_id: UUID
    email_address: str
    provider: str
    connected: bool

class PipelineStats(BaseModel):
    total_leads: int
    hot: int
    warm: int
    cold: int
    replied: int
    booked: int
    closed_won: int
    emails_sent_today: int
    followups_pending: int

class EmailLogResponse(BaseModel):
    id: UUID
    direction: str
    body: str
    sent_at: datetime
    ai_generated: bool
    class Config:
        from_attributes = True