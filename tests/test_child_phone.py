"""A phone a parent paired for their child sends the child's data and opens the child's screens. It changes nothing
that belongs to the parent's account: display choices, the terms they accepted, their first-run steps."""

from __future__ import annotations

from tests.test_accounts import _person, _phone, _signed_in_owner


def test_a_childs_phone_cant_change_the_parents_preferences(raw_client):
    client = _signed_in_owner(raw_client)
    kid = _person(client, "Kid", "child")
    before = client.get("/api/preferences").json()
    _, anon, token, screens = _phone(client, kid)
    r = anon.put("/api/preferences", json={"units": "us", "accent": "purple"}, headers=token)
    assert r.status_code == 403, r.text
    assert screens.put("/api/preferences", json={"units": "metric"}).status_code == 403
    assert client.get("/api/preferences").json() == before
    # It still reads them: the parent's choice of units is how the phone shows the child's data.
    assert anon.get("/api/preferences", headers=token).json()["accent"] == before["accent"]


def test_a_childs_phone_doesnt_accept_terms_or_finish_first_run_for_the_parent(raw_client):
    client = _signed_in_owner(raw_client)
    kid = _person(client, "Kid", "child")
    _, _, _, screens = _phone(client, kid)
    for path in ("/api/terms/accept", "/api/onboarding/done", "/api/onboarding/restart"):
        assert screens.post(path).status_code == 403, path
    # The parent's first-run steps aren't the child's phone's to show.
    assert client.post("/api/onboarding/restart").status_code == 200
    assert screens.get("/api/status").json()["onboarding"] is False
    assert client.get("/api/status").json()["onboarding"] is True


def test_the_parents_own_phone_changes_their_preferences_as_before(raw_client):
    client = _signed_in_owner(raw_client)
    _, anon, token, screens = _phone(client)
    assert anon.put("/api/preferences", json={"units": "us"}, headers=token).status_code == 200
    assert screens.put("/api/preferences", json={"accent": "teal"}).status_code == 200
    assert client.get("/api/preferences").json()["units"] == "us"
    assert client.get("/api/preferences").json()["accent"] == "teal"
    assert screens.post("/api/onboarding/done").status_code == 200
