"""
Global PII-safe HTTPException handling.

~34 call sites across retrieve/, export/, studies/, results/, and
projects/endpoints.py (plus identity/anon.py) raise `HTTPException(...,
detail=str(e))`, echoing whatever a Mosaiq/Pinnacle/ProKnow/Orthanc/psycopg2
exception happened to say -- which routinely quotes a real MRN, a server
filesystem path, or a raw date (docs/plans/pii-boundary-safety.md finding
#3). Patching each site individually leaves the same gap open for every
future one. This registers a single global handler instead: every
HTTPException's `detail`, if it's a string, is run through
`pii_patterns.redact()` before FastAPI serializes the response.

Generic pattern-only (dates/UIDs/paths/secrets) -- no real-id-aware
substitution, since a request-agnostic handler has no access to which real
ids were in scope for the failed request (docs/plans/pii-boundary-test-suite.md
§F, explicitly out of scope; a request-scoped precise version is future
work). This is a safety net for the *unexpected* case, on top of -- not
instead of -- the precise real_id/display_id substitution every call site
that already knows its own real id continues to do.

Both `fastapi.HTTPException` and `starlette.exceptions.HTTPException` are
registered explicitly. Starlette's own *lookup* at dispatch time does walk
the raised exception's MRO, so registering only the starlette base class
would already catch every `fastapi.HTTPException` raised across this
codebase's ~57 call sites (fastapi's subclasses starlette's). The reason
both still need registering is Starlette's own routing internals
(starlette/routing.py), which raise a *plain* `starlette.exceptions.
HTTPException` directly -- never via `fastapi.HTTPException` -- for a 404
(no route match) or 405 (method not allowed); that path only reaches our
handler because the base class is registered too.
"""
import logging

from fastapi import FastAPI, HTTPException
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler
from fastapi.requests import Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.src.common import pii_patterns

logger = logging.getLogger(__name__)


async def _pii_safe_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if isinstance(exc.detail, str) and exc.detail:
        redacted = pii_patterns.redact(exc.detail)
        if redacted != exc.detail:
            logger.warning(
                "Redacted PII-shaped content from an HTTPException detail before sending "
                "(status_code=%s)", exc.status_code,
            )
            exc = exc.__class__(status_code=exc.status_code, detail=redacted, headers=exc.headers)
    return await _default_http_exception_handler(request, exc)


def register_pii_safe_exception_handlers(app: FastAPI) -> None:
    """Call once, right after `app = FastAPI()`, before any router is included."""
    app.add_exception_handler(HTTPException, _pii_safe_http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, _pii_safe_http_exception_handler)
