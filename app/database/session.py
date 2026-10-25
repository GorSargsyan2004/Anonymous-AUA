"""Async database engine and session factory configuration."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sqlalchemy import text
from app.config import settings
from app.database.models import Base

# Create asynchronous SQLAlchemy engine
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Initialize database tables and apply column additions asynchronously on application startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Ensure recently added columns exist for existing deployments
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS completed_sessions_count INTEGER NOT NULL DEFAULT 0;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS target_sexes VARCHAR(64);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS target_orientations VARCHAR(128);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS show_typing BOOLEAN NOT NULL DEFAULT true;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT false;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE reports ADD COLUMN IF NOT EXISTS evidence_file_id VARCHAR(256);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS messages_count INTEGER NOT NULL DEFAULT 0;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS inactivity_warned BOOLEAN NOT NULL DEFAULT false;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS inactivity_warned_at TIMESTAMP WITH TIME ZONE;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS is_paused BOOLEAN NOT NULL DEFAULT false;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS paused_at TIMESTAMP WITH TIME ZONE;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS sex_updated_at TIMESTAMP WITH TIME ZONE;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS priority_delta_applied INTEGER NOT NULL DEFAULT 0;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(128);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT false;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_code VARCHAR(16);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_code_expires_at TIMESTAMP WITH TIME ZONE;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_attempts INTEGER NOT NULL DEFAULT 0;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_gender_verified BOOLEAN NOT NULL DEFAULT false;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS inferred_gender VARCHAR(16);"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS gender_mismatch BOOLEAN NOT NULL DEFAULT false;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS gender_appeal_pending BOOLEAN NOT NULL DEFAULT false;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_email_sent_at TIMESTAMP WITH TIME ZONE;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_emails_sent_today INTEGER NOT NULL DEFAULT 0;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_emails_reset_at TIMESTAMP WITH TIME ZONE;"
            )
        )
        # Backfill default preferences for existing users who do not have them set
        await conn.execute(
            text(
                """
                UPDATE users SET 
                    target_sexes = CASE 
                        WHEN sex = 'male' AND orientation = 'straight' THEN 'female'
                        WHEN sex = 'female' AND orientation = 'straight' THEN 'male'
                        WHEN sex = 'male' AND orientation = 'gay' THEN 'male'
                        WHEN sex = 'female' AND orientation = 'lesbian' THEN 'female'
                        WHEN sex = 'male' AND orientation = 'bisexual' THEN 'female,male'
                        WHEN sex = 'female' AND orientation = 'bisexual' THEN 'male,female'
                        WHEN sex = 'male' THEN 'female'
                        WHEN sex = 'female' THEN 'male'
                        ELSE 'male,female'
                    END,
                    target_orientations = CASE 
                        WHEN sex = 'male' AND orientation = 'straight' THEN 'straight,bisexual'
                        WHEN sex = 'female' AND orientation = 'straight' THEN 'straight,bisexual'
                        WHEN sex = 'male' AND orientation = 'gay' THEN 'gay,bisexual'
                        WHEN sex = 'female' AND orientation = 'lesbian' THEN 'lesbian,bisexual'
                        WHEN sex = 'male' AND orientation = 'bisexual' THEN 'straight,bisexual,gay'
                        WHEN sex = 'female' AND orientation = 'bisexual' THEN 'straight,bisexual,lesbian'
                        ELSE 'straight,bisexual'
                    END
                WHERE target_sexes IS NULL AND sex IS NOT NULL AND orientation IS NOT NULL;
                """
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS spam_flags_count INTEGER NOT NULL DEFAULT 0;"
            )
        )
        await conn.execute(
            text(
                """
                UPDATE users u SET spam_flags_count = COALESCE((
                    SELECT COUNT(DISTINCT f.from_user_id)
                    FROM feedbacks f
                    WHERE f.to_user_id = u.id AND f.feedback_type IN ('spam', 'not_aua')
                ), 0)
                WHERE spam_flags_count = 0;
                """
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_verification_reminder_at TIMESTAMP WITH TIME ZONE;"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_setup_reminder_at TIMESTAMP WITH TIME ZONE;"
            )
        )



@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for obtaining a managed database session."""
    session: AsyncSession = AsyncSessionLocal()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
