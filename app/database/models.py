"""SQLAlchemy 2.0 async database models for Anonymous AUA Bot."""

from datetime import datetime
from typing import Optional
import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for SQLAlchemy declarative models."""
    pass


class SexEnum(str, enum.Enum):
    """User sex enumeration."""
    MALE = "male"
    FEMALE = "female"


class OrientationEnum(str, enum.Enum):
    """User sexual orientation enumeration."""
    STRAIGHT = "straight"
    BISEXUAL = "bisexual"
    GAY = "gay"
    LESBIAN = "lesbian"


class FeedbackTypeEnum(str, enum.Enum):
    """Session feedback rating types."""
    LIKE = "like"
    DISLIKE = "dislike"
    SPAM = "spam"
    NOT_AUA = "not_aua"


class User(Base):
    """Represents a Telegram user registered in the Anonymous AUA system."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sex: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    orientation: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    is_searching: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    current_partner_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    target_sexes: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_orientations: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    show_typing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default=func.true())
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default=func.false(), index=True)
    completed_sessions_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    spam_flags_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sex_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_suggestion_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    email: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    is_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=func.false(), index=True
    )
    verification_code: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    verification_code_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    suggestions = relationship("Suggestion", back_populates="user", cascade="all, delete-orphan")

    def __init__(self, **kwargs):
        kwargs.setdefault("show_typing", True)
        kwargs.setdefault("is_banned", False)
        kwargs.setdefault("priority", 50)
        kwargs.setdefault("is_searching", False)
        kwargs.setdefault("completed_sessions_count", 0)
        kwargs.setdefault("spam_flags_count", 0)
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username} priority={self.priority}>"



class ChatSession(Base):
    """Represents an active or historic anonymous chat session between two users."""
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user1_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user2_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    messages_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        server_default=text("0"),
    )
    inactivity_warned: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=func.false(),
    )
    inactivity_warned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_paused: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=func.false(),
    )
    paused_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    feedbacks = relationship("Feedback", back_populates="session", cascade="all, delete-orphan")
    reports = relationship("Report", back_populates="session", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<ChatSession id={self.id} user1={self.user1_id} user2={self.user2_id} is_active={self.is_active}>"


class Feedback(Base):
    """User feedback left on a completed chat session."""
    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    from_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    to_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    feedback_type: Mapped[str] = mapped_column(String(32), nullable=False)
    priority_delta_applied: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    session = relationship("ChatSession", back_populates="feedbacks")

    __table_args__ = (
        UniqueConstraint("session_id", "from_user_id", name="uq_session_from_user"),
    )

    def __repr__(self) -> str:
        return f"<Feedback id={self.id} session_id={self.session_id} from={self.from_user_id} type={self.feedback_type}>"


class Report(Base):
    """Report submitted by a user against another user during/after a session."""
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    reporter_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reported_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_file_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    penalty_applied: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    session = relationship("ChatSession", back_populates="reports")

    def __repr__(self) -> str:
        return f"<Report id={self.id} session_id={self.session_id} reporter={self.reporter_id} reported={self.reported_id} resolved={self.is_resolved}>"


class Suggestion(Base):
    """User feedback and suggestions submitted to improve the bot."""
    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    suggestion_text: Mapped[str] = mapped_column(Text, nullable=False)
    admin_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_replied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    replied_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user = relationship("User", back_populates="suggestions")

    def __repr__(self) -> str:
        return f"<Suggestion id={self.id} user_id={self.user_id} replied={self.is_replied}>"


class ChatMessageMap(Base):
    """Bidirectional message ID mapping between partners in an active chat session."""
    __tablename__ = "chat_message_maps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    sender_msg_id: Mapped[int] = mapped_column(Integer, nullable=False)
    receiver_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    receiver_msg_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_cmm_sender", "sender_id", "sender_msg_id"),
        Index("ix_cmm_receiver", "receiver_id", "receiver_msg_id"),
    )

    def __repr__(self) -> str:
        return f"<ChatMessageMap id={self.id} session_id={self.session_id} sender={self.sender_id}:{self.sender_msg_id} -> receiver={self.receiver_id}:{self.receiver_msg_id}>"


class UserBlock(Base):
    """Tracks partner-to-partner blocks and temporary 'do not connect again' cooldowns."""
    __tablename__ = "user_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blocked_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "blocked_user_id", name="uq_user_blocked_pair"),
        Index("ix_ub_lookup", "user_id", "blocked_user_id", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<UserBlock id={self.id} user={self.user_id} blocked={self.blocked_user_id} expires={self.expires_at}>"


