from frontend_fastapi import email_backend


def test_send_mail_without_smtp_configured_logs_and_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(email_backend, "SMTP_HOST", "")
    with caplog.at_level("INFO"):
        email_backend.send_mail("Subject", "Body text", "someone@example.com")
    assert any("someone@example.com" in record.message for record in caplog.records)


def test_send_mail_with_smtp_configured_calls_smtplib(monkeypatch):
    sent = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def starttls(self):
            sent["starttls"] = True

        def login(self, username, password):
            sent["login"] = (username, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr(email_backend, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(email_backend, "SMTP_PORT", 587)
    monkeypatch.setattr(email_backend, "SMTP_USE_TLS", True)
    monkeypatch.setattr(email_backend, "SMTP_USERNAME", "hermes")
    monkeypatch.setattr(email_backend, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(email_backend.smtplib, "SMTP", _FakeSMTP)

    email_backend.send_mail("Subject", "Body text", "someone@example.com")

    assert sent["host"] == "smtp.example.com"
    assert sent["starttls"] is True
    assert sent["login"] == ("hermes", "secret")
    assert sent["message"]["To"] == "someone@example.com"


def test_send_mail_swallows_smtp_errors(monkeypatch):
    class _FailingSMTP:
        def __init__(self, *args, **kwargs):
            raise ConnectionRefusedError("no smtp server here")

    monkeypatch.setattr(email_backend, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(email_backend.smtplib, "SMTP", _FailingSMTP)

    # Must not raise -- a failed send shouldn't break the request that
    # triggered it (routers/accounts.py's invite flow already shows the
    # activation link via a flash message regardless).
    email_backend.send_mail("Subject", "Body text", "someone@example.com")
