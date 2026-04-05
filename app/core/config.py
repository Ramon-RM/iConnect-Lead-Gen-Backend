from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
from typing import Literal


class Settings(BaseSettings):
    # App
    app_name: str = "iConnect"
    app_env: Literal["development", "production"] = "development"
    secret_key: str = "change-this-in-production"
    debug: bool = True

    # Database
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    database_url: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # AI providers
    anthropic_api_key: str = ""
    groq_api_key: str = ""
    groq_model: str = "llama3-70b-8192"
    # Note: no AI_PROVIDER setting needed — auto-detected from which key is present
    # ANTHROPIC_API_KEY present → Claude | GROQ_API_KEY present → Groq

    # Gmail OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/gmail/callback"

    # Outlook OAuth
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_redirect_uri: str = "http://localhost:8000/api/auth/outlook/callback"
    microsoft_tenant_id: str = "common"

    # Polling intervals
    email_poll_interval_seconds: int = 60
    followup_check_interval_seconds: int = 300

    # Follow-up schedule
    followup_day_1: int = 1
    followup_day_2: int = 3
    followup_day_3: int = 7
    followup_day_4: int = 14

    # JWT
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()