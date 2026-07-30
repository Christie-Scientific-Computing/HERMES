"""
Database setup — runs Alembic migrations against the central Postgres
database and initializes the shared connection pool (backend/src/db.py).
"""
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from backend.src.db import init_pool

logger = logging.getLogger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def setup_status_db(database_url: str) -> None:
    init_pool(database_url)

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    logger.info("Running database migrations (alembic upgrade head)")
    command.upgrade(cfg, "head")
