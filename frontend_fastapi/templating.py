"""The single Jinja2Templates instance every router renders through."""
from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

from frontend_fastapi.settings import TEMPLATES_DIR

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def format_timestamp(value) -> str:
    """'YYYY-MM-DD at HH:MM:SS' -- the patient timeline's start_ts/end_ts
    arrive as ISO 8601 strings (FastAPI's own datetime -> JSON encoding),
    never as datetime objects, since backend_client just returns parsed
    JSON. Falsy (None, "") passes through as "" -- an in-flight attempt's
    end_ts, or a cancelled one that never started. Anything unparseable
    also falls back to the raw value rather than raising: one malformed
    timestamp must not 500 an entire patient's timeline."""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d at %H:%M:%S")
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d at %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def format_date(value) -> str:
    """'YYYY-MM-DD' -- for fields that are semantically a date despite being
    stored as TIMESTAMP(timezone=True) (e.g. expiry_date, always midnight,
    set via a date-only picker). Same fallback behaviour as format_timestamp:
    falsy -> "", unparseable -> raw value, never raises."""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(value)


def is_project_expired(project: dict) -> bool:
    """True iff `project` is approved but its expiry_date has passed --
    mirrors backend/src/projects/db_client.py's is_project_active guard
    (approved AND (expiry_date IS NULL OR expiry_date > now())), just
    inverted and client-side, since no endpoint returns this as a field
    (F007, .plans/ui-polish/plan.md). Used to pick status_badge's separate
    `expired` visual state -- a draft/submitted/rejected/revoked project is
    never "expired" regardless of any leftover expiry_date."""
    if project.get("status") != "approved":
        return False
    expiry = project.get("expiry_date")
    if not expiry:
        return False
    expiry_dt = datetime.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    if expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
    return expiry_dt <= datetime.now(timezone.utc)


templates.env.filters["hermes_timestamp"] = format_timestamp
templates.env.filters["hermes_date"] = format_date
templates.env.globals["is_project_expired"] = is_project_expired
