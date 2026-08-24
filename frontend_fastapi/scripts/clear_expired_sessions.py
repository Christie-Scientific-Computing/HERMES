"""
python -m frontend_fastapi.scripts.clear_expired_sessions

Deletes session rows past their expires_at. Nothing in the request path
ever does this (session_middleware.SessionMiddleware abandons an expired
session in-process and starts a fresh one, but never deletes the old row --
see its own logic), so left unrun, the sessions table grows by one orphan
row per expired session forever. Intended to run on a periodic schedule
(cron/systemd timer), the same operational shape as Django's own built-in
`manage.py clearsessions` for the equivalent problem in frontend/.
"""
import logging

from frontend_fastapi.database import SessionLocal
from frontend_fastapi.models import Session, utcnow

logging.basicConfig(level="INFO", format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def clear_expired_sessions() -> int:
    db = SessionLocal()
    try:
        deleted = db.query(Session).filter(Session.expires_at < utcnow()).delete(synchronize_session=False)
        db.commit()
        return deleted
    finally:
        db.close()


def main() -> None:
    deleted = clear_expired_sessions()
    logger.info("Deleted %d expired session row(s)", deleted)


if __name__ == "__main__":
    main()
