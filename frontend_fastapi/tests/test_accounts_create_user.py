from frontend_fastapi.models import User


def test_non_staff_user_cannot_create_accounts(client, make_user, login):
    make_user(username="alice", is_staff=False)
    login("alice")
    resp = client.get("/accounts/users/create")
    assert resp.status_code == 403


def test_staff_creates_an_immediately_usable_account(client, make_user, login, csrf_token, db):
    make_user(username="bob", is_staff=True)
    login("bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "a genuinely strong passphrase",
        "password2": "a genuinely strong passphrase", "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303

    new_user = db.query(User).filter_by(username="carol").one()
    assert new_user.is_active is True

    # Immediately usable -- no activation step.
    client.post("/accounts/logout", data={"csrf_token": csrf_token()})
    resp = client.post("/accounts/login", data={
        "username": "carol", "password": "a genuinely strong passphrase", "csrf_token": csrf_token(),
    }, follow_redirects=False)
    assert resp.status_code == 303


def test_mismatched_passwords_are_rejected(client, make_user, login, csrf_token):
    make_user(username="bob", is_staff=True)
    login("bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "a genuinely strong passphrase",
        "password2": "a totally different passphrase", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "didn&#39;t match" in resp.text or "didn't match" in resp.text


def test_password_too_short_is_rejected(client, make_user, login, csrf_token):
    make_user(username="bob", is_staff=True)
    login("bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "short1", "password2": "short1", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "too short" in resp.text


def test_password_too_similar_to_username_is_rejected(client, make_user, login, csrf_token):
    make_user(username="bob", is_staff=True)
    login("bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "carol carol carol", "password2": "carol carol carol",
        "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "too similar" in resp.text


def test_duplicate_username_is_rejected(client, make_user, login, csrf_token):
    make_user(username="bob", is_staff=True)
    make_user(username="carol")
    login("bob")
    resp = client.post("/accounts/users/create", data={
        "username": "carol", "password1": "a genuinely strong passphrase",
        "password2": "a genuinely strong passphrase", "csrf_token": csrf_token(),
    })
    assert resp.status_code == 400
    assert "already exists" in resp.text


def test_add_user_or_reject_catches_a_raced_duplicate_insert(db, SessionFactory):
    """_reject_duplicate_username is a plain check-then-insert -- it can't
    catch two concurrent submissions for the same username that both pass
    the check before either commits. _add_user_or_reject is the actual
    guarantee: simulates the race by landing a conflicting row through a
    SEPARATE session, after which point a naive db.add()+implicit-commit
    would raise an unhandled IntegrityError instead of a form error."""
    from frontend_fastapi.forms.accounts import CreateUserForm
    from frontend_fastapi.routers.accounts import _add_user_or_reject

    other_session = SessionFactory()
    other_session.add(User(username="carol", password_hash="x", is_active=True))
    other_session.commit()
    other_session.close()

    form = CreateUserForm(formdata=None, data={"username": "carol"})
    new_user = User(username="carol", password_hash="y", is_active=True)

    assert _add_user_or_reject(db, form, new_user) is False
    assert "already exists" in form.username.errors[0]
    # The failed insert must not have left the session unusable for
    # anything else this request goes on to do.
    assert db.query(User).filter_by(username="carol").count() == 1
