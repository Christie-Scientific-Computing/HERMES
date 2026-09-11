"""
FastAPI backend for Hermes. Exposes REST endpoints for importing, cleaning and exporting data
"""
import os
import logging
from fastapi import FastAPI, Header
from dotenv import load_dotenv
from backend.src.retrieve.endpoints import router as import_router
from backend.src.export.endpoints import router as export_router
from backend.src.database import setup_status_db
from backend.src.results.endpoints import router as results_router
from backend.src.studies.endpoints import router as studies_router
from backend.src.projects.endpoints import router as projects_router
from backend.src.admin.endpoints import router as admin_router
from backend.src.notifications.endpoints import router as notifications_router
from backend.src.error_reports.endpoints import router as error_reports_router
from backend.src.local_pacs.endpoints import router as local_pacs_router
from backend.src.common.errors import register_pii_safe_exception_handlers
from backend.src.identity import anon

load_dotenv()

#TODO load config file
# Setup FastAPI logging
logging.basicConfig(
        filename=None, 
        level="INFO",
        format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
        )
logging.getLogger('httpx').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


_INTERNAL_KEY = os.getenv("HERMES_INTERNAL_KEY")

DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    setup_status_db(DATABASE_URL)
else:
    logger.warning("DATABASE_URL not set; ABORTING!")
    exit()
app = FastAPI()
register_pii_safe_exception_handlers(app)
app.include_router(import_router)
app.include_router(export_router)
app.include_router(results_router)
app.include_router(studies_router)
app.include_router(projects_router)
app.include_router(admin_router)
app.include_router(notifications_router)
app.include_router(error_reports_router)
app.include_router(local_pacs_router)


@app.get("/health")
async def health(x_hermes_internal_key: str | None = Header(default=None)) -> dict:
    """Unauthenticated liveness check (status alone) -- a process
    supervisor/load balancer without the internal key still gets this.
    `anonymisation_active` additionally lets a caller (frontend_fastapi's
    status badge) show whether anonymisation is configured, but is only
    included for a caller presenting the correct internal key when one is
    configured -- same opt-in-if-set idiom as verify_internal_key, since
    even this boolean is topology information we don't want an
    unauthenticated caller on the DMZ proxy able to query for free."""
    body = {"status": "ok"}
    if _INTERNAL_KEY is None or x_hermes_internal_key == _INTERNAL_KEY:
        body["anonymisation_active"] = anon.is_configured()
    return body