"""
Flash messages: one-shot notices that survive exactly one redirect (e.g.
"Project submitted for review" after a POST-redirect-GET), stored on the
session row rather than in the URL or a cookie of their own. Port of
Django's contrib.messages framework, scoped down to what this codebase
actually uses (a tag + text pair, no extra levels/tags machinery).
"""
from typing import Literal

from frontend_fastapi.models import Session

FlashTag = Literal["success", "error", "warning", "info"]


def flash(session: Session, tag: FlashTag, text: str) -> None:
    # In-place mutation on session.flash_messages (a MutableList-wrapped JSON
    # column, see models.py) -- this is what SQLAlchemy actually tracks;
    # reassigning session.flash_messages = [...] would work too, but a
    # helper that always appends in place avoids that footgun entirely.
    session.flash_messages.append({"tag": tag, "text": text})


def pop_flashes(session: Session) -> list[dict]:
    """Read-then-clear: call once per response (deps.get_template_context
    does this), so a flash message renders exactly once even if the same
    session loads another page immediately after."""
    messages = list(session.flash_messages)
    session.flash_messages.clear()
    return messages
