from frontend_fastapi import security


def test_hash_and_verify_password_roundtrip():
    hashed = security.hash_password("correct horse battery staple")
    assert security.verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = security.hash_password("correct horse battery staple")
    assert security.verify_password("wrong password", hashed) is False


def test_verify_password_rejects_malformed_hash_without_raising():
    assert security.verify_password("anything", "not-a-real-argon2-hash") is False


def test_new_session_id_and_csrf_token_are_distinct_each_call():
    assert security.new_session_id() != security.new_session_id()
    assert security.new_csrf_token() != security.new_csrf_token()


def test_account_token_roundtrip_valid():
    token = security.make_account_token(user_id=42, password_hash="hash-v1")
    data = security.read_account_token(token)
    assert data is not None
    assert data["uid"] == 42
    assert security.account_token_matches(data, current_password_hash="hash-v1")


def test_account_token_tampered_signature_rejected():
    token = security.make_account_token(user_id=42, password_hash="hash-v1")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert security.read_account_token(tampered) is None


def test_account_token_expired_rejected(monkeypatch):
    # Negative max-age guarantees any elapsed signing age (>= 0) exceeds it,
    # deterministically forcing SignatureExpired without needing a real sleep.
    monkeypatch.setattr(security, "_ACCOUNT_TOKEN_MAX_AGE_SECONDS", -1)
    token = security.make_account_token(user_id=42, password_hash="hash-v1")
    assert security.read_account_token(token) is None


def test_account_token_invalidated_once_password_changes():
    """The property that makes an invite/activate/reset token effectively
    single-use: once the account's password_hash actually changes, a token
    signed against the OLD hash must stop matching -- even though it's
    still a validly-signed, unexpired token."""
    token = security.make_account_token(user_id=42, password_hash="hash-v1")
    data = security.read_account_token(token)
    assert data is not None
    assert security.account_token_matches(data, current_password_hash="hash-v2-after-activation") is False
