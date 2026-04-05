from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings

# Convert standard postgres URL to async asyncpg URL
db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(db_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# For Celery tasks
from contextlib import asynccontextmanager
@asynccontextmanager
async def get_db_session():
    async with AsyncSessionLocal() as session:
        yield session