"""
Minimal email sending -- one function, two behaviors picked by whether
SMTP_HOST is set. Mirrors frontend/hermes_frontend/settings.py's EMAIL_BACKEND
switch (console vs real SMTP) without needing Django's whole mail framework
for the one email this rewrite currently sends (the invite activation link).
"""
import logging
import smtplib
from email.message import EmailMessage

from frontend_fastapi.settings import (
    SMTP_FROM_ADDRESS,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USERNAME,
)

logger = logging.getLogger(__name__)


def send_mail(subject: str, body: str, to: str) -> None:
    """Best-effort -- never raises. A failed/unconfigured send must not
    block the request that triggered it (routers/accounts.py's invite_user
    already shows the same information via a flash message regardless)."""
    if not SMTP_HOST:
        logger.info("SMTP not configured; email not sent. To: %s Subject: %s\n%s", to, subject, body)
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM_ADDRESS
    message["To"] = to
    message.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
    except Exception:
        logger.exception("Failed to send email to %s", to)
