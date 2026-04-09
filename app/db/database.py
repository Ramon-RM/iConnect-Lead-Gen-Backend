from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings
from contextlib import asynccontextmanager

# 1. CRITICAL: Keep your original URL converter so Python doesn't crash
db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")

# 2. Add the fix to disable the prepared statement cache for Supabase
engine = create_async_engine(
    db_url, 
    echo=False,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# For Celery / Background tasks
@asynccontextmanager
async def get_db_session():
    async with AsyncSessionLocal() as session:
        yield session