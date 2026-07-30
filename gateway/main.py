"""
HERMES Gateway — FastAPI app.

Everything this service used to proxy through to the backend (studies,
import, export, results) has been absorbed directly into the merged
backend, fronted by the thin reverse proxy in proxy/ instead. What's left
here is PACS comparison (routers/pacs.py + pacs_client.py) — genuinely
frontend-only functionality today, parked and unwired until the Django
frontend work resumes, at which point it'll likely move to live inside
Django directly (mirroring how the old Streamlit UI called it).

Run from the gateway/ directory:
    fastapi run main.py --port 8001
"""
import os
import logging

from dotenv import load_dotenv
from fastapi import FastAPI

from routers.pacs import router as pacs_router

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="HERMES Gateway (PACS only)",
    description="PACS comparison endpoints only -- everything else has moved to the backend + proxy.",
    version="0.2.0",
)

app.include_router(pacs_router)  # GET /pacs/status, POST /pacs/query_series, /pacs/query_studies
