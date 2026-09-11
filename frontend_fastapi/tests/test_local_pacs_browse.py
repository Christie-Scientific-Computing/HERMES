"""
Tests for F002 (routers/local_pacs.py's browse/search + series partial).
conquest_client (F001) is monkeypatched at its module boundary -- an actual
C-FIND round trip is F001's own test suite's job (test_conquest_client.py),
per that module's Testing considerations.
"""
from unittest.mock import Mock

import pytest

from frontend_fastapi.local_pacs import conquest_client as cc


def _found(matches, truncated=False):
    return cc.FindResult(matches=matches, truncated=truncated)


@pytest.fixture()
def mock_cc(monkeypatch):
    monkeypatch.setattr(cc, "is_configured", Mock(return_value=True))
    monkeypatch.setattr(cc, "find_studies", Mock(return_value=_found([])))
    monkeypatch.setattr(cc, "find_series", Mock(return_value=[]))
    return cc


def test_browse_page_redirects_when_not_logged_in(client):
    resp = client.get("/local_pacs", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


def test_landing_on_the_page_with_no_query_at_all_does_not_search(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")

    resp = client.get("/local_pacs")

    assert resp.status_code == 200
    cc.find_studies.assert_not_called()


def test_submitting_the_form_with_every_field_blank_is_rejected_with_a_clear_message(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")

    resp = client.get("/local_pacs", params={"submitted": "1"})

    assert resp.status_code == 200
    assert b"Provide at least one search field" in resp.content
    cc.find_studies.assert_not_called()


def test_search_by_patient_id_shows_results(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")
    cc.find_studies.return_value = _found([{
        "patient_id": "ANON1", "study_instance_uid": "1.2.3", "study_date": "20260101",
        "study_description": "Planning CT", "modalities_in_study": "CT", "accession_number": "ACC1",
        "series_count": "2",
    }])

    resp = client.get("/local_pacs", params={"patient_id": "ANON1", "submitted": "1"})

    assert resp.status_code == 200
    assert b"ANON1" in resp.content
    assert b"Planning CT" in resp.content
    cc.find_studies.assert_called_once()


def test_search_past_the_result_cap_shows_a_truncation_notice(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")
    cc.find_studies.return_value = _found([{
        "patient_id": "ANON1", "study_instance_uid": "1.2.3", "study_date": "20260101",
        "study_description": "Planning CT", "modalities_in_study": "CT", "accession_number": "ACC1",
        "series_count": "2",
    }], truncated=True)

    resp = client.get("/local_pacs", params={"patient_id": "ANON1", "submitted": "1"})

    assert resp.status_code == 200
    assert b"Showing the first 200" in resp.content


def test_search_with_no_matches_shows_no_results_state(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")

    resp = client.get("/local_pacs", params={"patient_id": "NOBODY", "submitted": "1"})

    assert resp.status_code == 200
    assert b"No results found" in resp.content


def test_unreachable_conquest_shows_a_distinct_error_not_empty_results(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")
    cc.find_studies.side_effect = cc.ConquestUnreachable("Could not connect to local PACS at host:1234")

    resp = client.get("/local_pacs", params={"patient_id": "ANON1", "submitted": "1"})

    assert resp.status_code == 200
    assert b"Local PACS unavailable" in resp.content
    assert b"No results found" not in resp.content


def test_unconfigured_conquest_shows_a_clear_error(client, make_user, login, monkeypatch):
    make_user("alice")
    login("alice")
    monkeypatch.setattr(cc, "is_configured", Mock(return_value=False))

    resp = client.get("/local_pacs", params={"patient_id": "ANON1", "submitted": "1"})

    assert resp.status_code == 200
    assert b"not configured" in resp.content


def test_series_partial_lists_series(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")
    cc.find_series.return_value = [{
        "series_instance_uid": "1.2.3.1", "modality": "CT", "series_description": "Axials", "instance_count": "150",
    }]

    resp = client.get("/local_pacs/series/1.2.3")

    assert resp.status_code == 200
    assert b"Axials" in resp.content
    cc.find_series.assert_called_once_with(study_instance_uid="1.2.3")


def test_move_action_hidden_from_non_custodians(client, make_user, login, mock_cc):
    make_user("alice", is_staff=False)
    login("alice")
    cc.find_studies.return_value = _found([{
        "patient_id": "ANON1", "study_instance_uid": "1.2.3", "study_date": "20260101",
        "study_description": "Planning CT", "modalities_in_study": "CT", "accession_number": "ACC1",
        "series_count": "2",
    }])

    resp = client.get("/local_pacs", params={"patient_id": "ANON1", "submitted": "1"})

    assert b"name=\"destination_id\"" not in resp.content


# ---- Round-2 F001: connectivity indicator ----

def test_landing_on_the_page_shows_the_connectivity_slot_not_results(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")

    resp = client.get("/local_pacs")

    assert resp.status_code == 200
    assert b'hx-get="/local_pacs/status"' in resp.content
    assert b"No results found" not in resp.content
    assert b"Local PACS unavailable" not in resp.content


def test_a_search_does_not_show_the_connectivity_slot(client, make_user, login, mock_cc):
    make_user("alice")
    login("alice")

    resp = client.get("/local_pacs", params={"patient_id": "NOBODY", "submitted": "1"})

    assert resp.status_code == 200
    assert b'hx-get="/local_pacs/status"' not in resp.content


def test_status_endpoint_requires_login(client):
    resp = client.get("/local_pacs/status", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)


def test_status_endpoint_reports_reachable(client, make_user, login, monkeypatch):
    make_user("alice")
    login("alice")
    monkeypatch.setattr(cc, "is_configured", Mock(return_value=True))
    monkeypatch.setattr(cc, "echo", Mock(return_value=None))

    resp = client.get("/local_pacs/status")

    assert resp.status_code == 200
    assert b"Local PACS reachable" in resp.content
    cc.echo.assert_called_once()


def test_status_endpoint_reports_unreachable_with_the_failure_reason(client, make_user, login, monkeypatch):
    make_user("alice")
    login("alice")
    monkeypatch.setattr(cc, "is_configured", Mock(return_value=True))
    monkeypatch.setattr(cc, "echo", Mock(side_effect=cc.ConquestTimeout("Local PACS did not respond in time.")))

    resp = client.get("/local_pacs/status")

    assert resp.status_code == 200
    assert b"Local PACS unavailable" in resp.content
    assert b"did not respond in time" in resp.content


def test_status_endpoint_reports_not_configured_without_attempting_echo(client, make_user, login, monkeypatch):
    make_user("alice")
    login("alice")
    monkeypatch.setattr(cc, "is_configured", Mock(return_value=False))
    monkeypatch.setattr(cc, "echo", Mock())

    resp = client.get("/local_pacs/status")

    assert resp.status_code == 200
    assert b"not configured" in resp.content
    cc.echo.assert_not_called()
