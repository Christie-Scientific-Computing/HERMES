"""The single Jinja2Templates instance every router renders through."""
from datetime import datetime

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


templates.env.filters["hermes_timestamp"] = format_timestamp
