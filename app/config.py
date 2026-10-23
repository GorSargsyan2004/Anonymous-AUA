"""Configuration settings loaded from environment variables using pydantic-settings."""

from typing import Any, List, Optional, Union
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings class."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram Bot Configuration
    BOT_TOKEN: str = Field(..., description="Telegram Bot Token obtained from BotFather")
    ADMIN_IDS: Any = Field(
        default_factory=list,
        description="Comma-separated Telegram user IDs of bot administrators",
    )
    MAX_MEDIA_FILE_SIZE_MB: int = Field(
        default=100,
        description="Max allowed media file size in megabytes before rejection",
    )
    TELEGRAM_API_ID: Optional[int] = Field(
        default=None,
        description="Telegram App API ID for local bot API server",
    )
    TELEGRAM_API_HASH: Optional[str] = Field(
        default=None,
        description="Telegram App API Hash for local bot API server",
    )
    TELEGRAM_BOT_API_URL: Optional[str] = Field(
        default=None,
        description="Custom Telegram Bot API URL (e.g. http://telegram-bot-api:8081/bot)",
    )

    # PostgreSQL Database Configuration
    POSTGRES_USER: str = Field(default="aua_user", description="PostgreSQL username")
    POSTGRES_PASSWORD: str = Field(default="aua_secure_password_here", description="PostgreSQL password")
    POSTGRES_DB: str = Field(default="aua_chat_db", description="PostgreSQL database name")
    POSTGRES_HOST: str = Field(default="postgres", description="PostgreSQL host name or IP")
    POSTGRES_PORT: int = Field(default=5432, description="PostgreSQL port")

    # Anti-Cheat / Anti-Exploit Safeguards
    SEX_CHANGE_COOLDOWN_HOURS: int = Field(
        default=48,
        description="Hours a user must wait before changing their profile sex again (2 days)",
    )
    REMATCH_COOLDOWN_MINUTES: int = Field(
        default=20,
        description="Minutes before two users who recently chatted can match with each other again",
    )
    SUGGESTION_COOLDOWN_MINUTES: int = Field(
        default=5,
        description="Minutes a user must wait between submitting suggestions",
    )

    # University Email Verification & SMTP
    SMTP_HOST: Optional[str] = Field(default=None, description="SMTP server host for sending verification emails")
    SMTP_PORT: int = Field(default=587, description="SMTP server port")
    SMTP_USER: Optional[str] = Field(default=None, description="SMTP username")
    SMTP_PASSWORD: Optional[str] = Field(default=None, description="SMTP password")
    SMTP_FROM_EMAIL: Optional[str] = Field(default=None, description="Sender email address")
    SMTP_USE_TLS: bool = Field(default=True, description="Whether to use STARTTLS for SMTP")
    VERIFICATION_EMAIL_COOLDOWN_MINUTES: int = Field(
        default=3,
        description="Minutes a user must wait before requesting another OTP verification email",
    )
    VERIFICATION_DAILY_LIMIT: int = Field(
        default=5,
        description="Maximum verification emails a user can request in a 24-hour period",
    )
    VERIFICATION_GLOBAL_RATE_LIMIT: int = Field(
        default=20,
        description="Maximum outbound verification emails allowed across the bot within 5 minutes",
    )

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: Any) -> List[int]:
        """Parse comma-separated strings, ints, or raw lists into a list of integer admin IDs."""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                import json
                try:
                    return [int(x) for x in json.loads(v)]
                except Exception:
                    pass
            return [int(item.strip()) for item in v.split(",") if item.strip()]
        elif isinstance(v, (int, float)):
            return [int(v)]
        elif isinstance(v, (list, tuple, set)):
            return [int(item) for item in v]
        return []

    @property
    def admin_ids(self) -> List[int]:
        """Convenience property returning ADMIN_IDS as a list of integers."""
        if isinstance(self.ADMIN_IDS, list):
            return self.ADMIN_IDS
        return [int(x) for x in self.ADMIN_IDS]


    @property
    def database_url(self) -> str:
        """Construct the async PostgreSQL connection string for SQLAlchemy asyncpg."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


settings = Settings()
