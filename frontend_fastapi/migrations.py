"""
Runs this project's own Alembic migrations at startup (main.py's lifespan) --
mirrors backend/src/database.py's setup_status_db exactly, against this
project's own local database instead of HermesDB. See settings.py's
DATABASE_URL docstring for why these must never be the same database.
"""
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"


def run_migrations(database_url: str) -> None:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", database_url)
    logger.info("Running frontend_fastapi database migrations (alembic upgrade head)")
    command.upgrade(cfg, "head")
