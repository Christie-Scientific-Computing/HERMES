"""
HERMES Gateway — FastAPI app.

Sits in the DMZ / external network. Provides explicitly documented API
endpoints for study discovery, import, export, and results, plus a
catch-all proxy that transparently forwards anything else to the Hermes backend.

The browser frontend is a separate Streamlit process (see ui/Home.py).

Run both from the gateway/ directory:
    fastapi run main.py --port 8001
    streamlit run ui/Home.py
"""
import os
import logging
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI

from routers.studies  import router as studies_router
from routers.import_  import router as import_router
from routers.export_  import router as export_router
from routers.results_ import router as results_router
from routers.forward  import router as forward_router

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

HERMES_URL = os.getenv("HERMES_URL")
if not HERMES_URL:
    logger.error("HERMES_URL is not set — cannot start gateway")
    raise SystemExit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(base_url=HERMES_URL, timeout=None)
    logger.info("Gateway started. Forwarding to %s", HERMES_URL)
    yield
    await app.state.client.aclose()
    logger.info("Gateway shut down")


app = FastAPI(
    title="HERMES Gateway",
    description=(
        "User-facing gateway for the HERMES radiotherapy data pipeline. "
        "Provides study discovery, import, export, and results endpoints. "
        "A Streamlit frontend is available as a separate process (ui/Home.py)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Specific routers must be registered before the catch-all forward router
app.include_router(studies_router)  # GET /studies, GET /studies/{id}
app.include_router(import_router)   # POST /import/*
app.include_router(export_router)   # GET|POST /export/*
app.include_router(results_router)  # GET /results/*
app.include_router(forward_router)  # catch-all /{path:path} — must be last
