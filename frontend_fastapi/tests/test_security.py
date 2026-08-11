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
    # Flips the FIRST character of the signature segment, not the last --
    # base64's final character can sit in a padding-affected position where
    # more than one character decodes to the same bytes (depends on the
    # signature's length mod 4), making a last-character flip a flaky,
    # sometimes-no-op tamper. The first character of any base64 group has
    # no such ambiguity: a different value always changes the decoded byte.
    header, timestamp, signature = token.rsplit(".", 2)
    tampered_signature = ("a" if signature[0] != "a" else "b") + signature[1:]
    tampered = f"{header}.{timestamp}.{tampered_signature}"
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


def test_unusable_password_never_verifies_against_anything():
    sentinel = security.unusable_password()
    assert security.is_usable_password(sentinel) is False
    assert security.verify_password("", sentinel) is False
    assert security.verify_password("some guess", sentinel) is False


def test_usable_password_is_a_real_hash():
    hashed = security.hash_password("correct horse battery staple")
    assert security.is_usable_password(hashed) is True


def test_unusable_password_sentinels_are_distinct():
    assert security.unusable_password() != security.unusable_password()


def test_password_strength_rejects_short_passwords():
    errors = security.password_strength_errors("short1")
    assert any("too short" in e for e in errors)


def test_password_strength_accepts_a_reasonable_password():
    assert security.password_strength_errors("a genuinely strong passphrase") == []


def test_password_strength_rejects_password_containing_username():
    errors = security.password_strength_errors("carol carol carol", username="carol")
    assert any("too similar to the username" in e for e in errors)


def test_password_strength_rejects_password_containing_email_local_part():
    errors = security.password_strength_errors("carolwilson is great", email="carolwilson@example.com")
    assert any("too similar to the email" in e for e in errors)


def test_password_strength_ignores_short_email_local_parts():
    # A 2-character local part ("cw@...") is too likely to appear in an
    # unrelated password by coincidence to be a meaningful similarity signal.
    errors = security.password_strength_errors("cwaitforitlongpassword", email="cw@example.com")
    assert errors == []


def test_password_strength_rejects_password_containing_first_name():
    """Matches Django's UserAttributeSimilarityValidator, which checks
    first_name/last_name too, not just username/email."""
    errors = security.password_strength_errors("sarahconnor1", first_name="Sarah")
    assert any("too similar to the first name" in e for e in errors)


def test_password_strength_rejects_password_containing_last_name():
    errors = security.password_strength_errors("connorpassword", last_name="Connor")
    assert any("too similar to the last name" in e for e in errors)
