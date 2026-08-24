"""
SQLAlchemy models for this project's own local database.

Three tables, matching what Django's contrib apps (auth, sessions) plus
research_projects.ProjectDocument gave for free in the frontend/ (Django)
version of this app -- see docs/frontend-rewrite-implementation-plan.md
Phase 0. Nothing here is HermesDB (job/event/project) data; that stays
backend-owned and is only ever read/written through backend_client.py.

User merges what used to be Django's auth.User + accounts.Profile into one
table -- there's no Django User model to attach a Profile to here.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware_utc(dt: datetime) -> datetime:
    """Normalizes a DateTime(timezone=True) value read back from the DB to
    a UTC-aware datetime before comparing it against utcnow(). Needed
    because SQLite (this project's default/dev database) has no native
    timezone-aware storage -- SQLAlchemy's sqlite dialect silently returns
    a naive datetime on read regardless of how the column is declared,
    which raises TypeError when compared against an aware one. Postgres
    round-trips aware datetimes correctly on its own, so this is a no-op
    there; safe to call unconditionally either way."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254), default="")
    first_name: Mapped[str] = mapped_column(String(150), default="")
    last_name: Mapped[str] = mapped_column(String(150), default="")
    department: Mapped[str] = mapped_column(String(200), default="")  # matches Django's Profile.department max_length
    password_hash: Mapped[str] = mapped_column(String(255))
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    """
    A browser session. Anonymous visitors get a row too (user_id NULL) --
    that's what makes CSRF protection and flash messages work before
    login, exactly like Django's session framework. See
    session_middleware.SessionMiddleware for how the row is loaded/created,
    deps.get_session for how the rest of a request reads it, and
    auth.login_user for how login rotates it (a fresh id, never reusing a
    pre-login session id).
    """
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(43), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    csrf_token: Mapped[str] = mapped_column(String(43))
    # MutableList.as_mutable is required, not cosmetic: a bare JSON column
    # doesn't notify SQLAlchemy's unit-of-work of in-place mutation, so
    # flash.flash()'s session.flash_messages.append(...) would silently
    # never be persisted without this wrapper.
    flash_messages: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped["User | None"] = relationship()


class ProjectDocument(Base):
    """
    Ethics-approval document uploads. project_id is a plain string, not a
    foreign key -- there is no local Project model here (research_projects
    data is backend-owned, fetched fresh via the API), matching the
    Django version's identical design (frontend/research_projects/models.py).
    """
    __tablename__ = "project_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255))
    uploaded_by: Mapped[str] = mapped_column(String(150))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
