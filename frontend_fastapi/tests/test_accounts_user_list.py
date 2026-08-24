def test_non_staff_user_cannot_view_the_list(client, make_user, login):
    make_user(username="alice", is_staff=False)
    login("alice")
    resp = client.get("/accounts/users")
    assert resp.status_code == 403


def test_staff_user_sees_every_user(client, make_user, login):
    make_user(username="bob", is_staff=True)
    make_user(username="alice", is_staff=False)
    login("bob")
    resp = client.get("/accounts/users")
    assert resp.status_code == 200
    assert "bob" in resp.text
    assert "alice" in resp.text
