"""
HERMES Proxy — thin reverse proxy for the DMZ / external network.

Forwards everything to the HERMES backend, on the internal network.
Deliberately has no business logic and no database of its own: PACS
comparison and anonymisation (the only two things the old `gateway`
service did beyond proxying) now live inside the backend, since the
external anon-mapping database is reachable directly from the backend's
network. Real patient IDs never cross this proxy -- the backend only
ever sends/receives anon ids across this boundary.

Run from the proxy/ directory:
    fastapi run main.py --port 8001
"""
import os
import logging
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request

from forward import proxy_request

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

HERMES_URL = os.getenv("HERMES_URL")
if not HERMES_URL:
    logger.error("HERMES_URL is not set — cannot start proxy")
    raise SystemExit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(base_url=HERMES_URL, timeout=None)
    logger.info("Proxy started. Forwarding to %s", HERMES_URL)
    yield
    await app.state.client.aclose()
    logger.info("Proxy shut down")


app = FastAPI(
    title="HERMES Proxy",
    description=(
        "Thin reverse proxy for the HERMES radiotherapy data pipeline. "
        "Forwards every request to the backend on the internal network; "
        "carries no business logic of its own."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def forward(request: Request, path: str):
    return await proxy_request(request, path)
