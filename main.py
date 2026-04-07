"""
iConnect — Main FastAPI App
"""
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.core.config import settings
from app.api.routes.routes import router
from app.api.routes.outbound_routes import router as outbound_router
from app.services.scheduler.tasks import start_scheduler
from app.services.email import gmail_service
from app.services.email.processor import process_inbound_email
from app.models.models import ClientAccount
from app.db.database import get_db
import os
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi import Request
from typing import Optional

# Load your .env keys
load_dotenv()

# Connect to Supabase
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

class CampaignCreate(BaseModel):
    name: str
    niche: Optional[str] = None
    pain_point_focus: Optional[str] = None
    value_proposition: Optional[str] = None
    social_proof: Optional[str] = None
    cta: Optional[str] = "open to a 15-minute chat?"
    daily_send_limit: Optional[int] = 30

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown events."""
    from app.services.ai.ai_service import _ACTIVE_PROVIDER
    print(f"🚀 iConnect starting | env: {settings.app_env} | AI provider: {_ACTIVE_PROVIDER.upper()}")
    
    # Start the background scheduler heartbeat!
    start_scheduler()
    
    yield
    print("iConnect shutting down")

app = FastAPI(
    title="iConnect",
    description="AI-powered B2B email automation — lead capture, reply, follow-up.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — update origins in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(outbound_router) # Moved this here so it works!

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "ai_provider": settings.ai_provider if hasattr(settings, "ai_provider") else "auto",
    }

@app.post("/api/trigger-inbox-scan")
async def trigger_inbox_scan(client_id: UUID, db: AsyncSession = Depends(get_db)):
    """Manually tell the system to check Gmail and run the AI."""
    
    client = await db.get(ClientAccount, client_id)
    if not client:
        return {"error": "Client not found"}

    try:
        unread_emails = gmail_service.fetch_new_emails(client)
    except AttributeError:
        return {"error": "Check gmail_service.py for the exact fetch function name and update main.py"}

    if not unread_emails:
        return {"status": "No new emails found."}

    count = 0
    for email_data in unread_emails:
        await process_inbound_email(db, client, email_data)
        count += 1

    return {"status": f"Success! AI processed and replied to {count} emails."}

@app.post("/api/outbound/campaigns")
async def create_campaign(campaign: CampaignCreate, request: Request):
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return {"error": "Not authenticated"}

    token = auth_header.replace('Bearer ', '')

    try:
        user_response = supabase.auth.get_user(token)
        user_id = user_response.user.id
    except Exception as e:
        return {"error": f"Auth failed: {str(e)}"}

    db_data = {
        "name": campaign.name,
        "niche": campaign.niche,
        "pain_point_focus": campaign.pain_point_focus,
        "value_proposition": campaign.value_proposition,
        "social_proof": campaign.social_proof,
        "cta": campaign.cta,
        "daily_send_limit": campaign.daily_send_limit,
        "user_id": user_id,
        "status": "draft",
        "total_prospects": 0,
        "emails_sent": 0,
        "replies_received": 0,
        "reply_rate": 0,
    }

    try:
        result = supabase.table("campaigns").insert(db_data).execute()
        return result.data[0]
    except Exception as e:
        return {"error": f"Database insert failed: {str(e)}"}
    
    
@app.get("/api/outbound/campaigns")
async def list_campaigns(request: Request):
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return []

    token = auth_header.replace('Bearer ', '')

    try:
        user_response = supabase.auth.get_user(token)
        user_id = user_response.user.id
        result = supabase.table("campaigns").select("*").eq("user_id", user_id).execute()
        return result.data
    except Exception as e:
        return []