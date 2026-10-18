"""Configuration settings loaded from environment variables using pydantic-settings."""

from typing import Any, List, Union
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

    # PostgreSQL Database Configuration
    POSTGRES_USER: str = Field(default="aua_user", description="PostgreSQL username")
    POSTGRES_PASSWORD: str = Field(default="aua_secure_password_here", description="PostgreSQL password")
    POSTGRES_DB: str = Field(default="aua_chat_db", description="PostgreSQL database name")
    POSTGRES_HOST: str = Field(default="postgres", description="PostgreSQL host name or IP")
    POSTGRES_PORT: int = Field(default=5432, description="PostgreSQL port")

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
