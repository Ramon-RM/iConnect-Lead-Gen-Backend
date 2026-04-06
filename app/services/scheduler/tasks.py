"""
iConnect — APScheduler Tasks
Background workers running directly inside FastAPI:
  - poll_all_inboxes: runs every 60s
  - process_followups: runs every 5min
  - process_outbound_campaigns: runs every 15min
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.core.config import settings

logger = logging.getLogger(__name__)

# Create the scheduler instance
scheduler = AsyncIOScheduler()

async def poll_all_inboxes():
    """Fetch new emails for all active, OAuth-connected clients."""
    from app.db.database import get_db
    from app.models.models import ClientAccount
    from app.services.email import gmail_service
    from app.services.email.processor import process_inbound_email
    from sqlalchemy import select

    # get_db is a generator, so we use an async for loop to extract the session safely
    async for db in get_db():
        try:
            result = await db.execute(
                select(ClientAccount).where(
                    ClientAccount.is_active == True,
                    ClientAccount.oauth_connected == True,
                )
            )
            clients = result.scalars().all()
            
            for client in clients:
                try:
                    if client.email_provider == "gmail":
                        emails = gmail_service.fetch_new_emails(
                            client=client,
                            since_timestamp=client.updated_at,
                        )
                        for raw_email in emails:
                            await process_inbound_email(db, client, raw_email)
                except Exception as e:
                    logger.error(f"Polling failed for client {client.id}: {e}")
        except Exception as e:
            logger.error(f"Database error in poll_all_inboxes: {e}")


async def process_followups():
    """Check follow-up queue and send any due emails."""
    from app.db.database import get_db
    from app.services.email.processor import process_due_followups
    
    async for db in get_db():
        try:
            await process_due_followups(db)
        except Exception as e:
            logger.error(f"Failed to process followups: {e}")


async def process_outbound_campaigns():
    """Fire cold emails for all active campaigns."""
    from app.db.database import get_db
    from app.services.email.outbound_processor import process_campaign_sends
    
    async for db in get_db():
        try:
            await process_campaign_sends(db)
        except Exception as e:
            logger.error(f"Failed to process outbound campaigns: {e}")


def start_scheduler():
    """Wire up the timers and start the engine."""
    # Run every 60 seconds
    scheduler.add_job(poll_all_inboxes, 'interval', seconds=60)
    
    # Run every 5 minutes
    scheduler.add_job(process_followups, 'interval', minutes=5)
    
    # Run every 30 minutes
    scheduler.add_job(process_outbound_campaigns, 'interval', minutes=15)
    
    scheduler.start()
    logger.info("🚀 APScheduler background engine started successfully!")