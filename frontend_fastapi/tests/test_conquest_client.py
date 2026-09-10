"""
Tests for F001 (frontend_fastapi/local_pacs/conquest_client.py). Uses a
hand-rolled pynetdicom test SCP (no real Conquest instance needed, per that
module's own Testing considerations) rather than mocking pynetdicom itself,
so this actually exercises real DICOM wire behaviour.
"""
import socket
import threading

import pytest
from pydicom.dataset import Dataset
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelMove,
    Verification,
)

from frontend_fastapi import settings
from frontend_fastapi.local_pacs import conquest_client as cc


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _handle_find(event):
    match = Dataset()
    match.PatientID = "ANON1"
    match.StudyInstanceUID = "1.2.3.4"
    match.StudyDate = "20260101"
    match.StudyDescription = "Test study"
    match.ModalitiesInStudy = "CT"
    match.AccessionNumber = "ACC1"
    match.NumberOfStudyRelatedSeries = "2"
    yield 0xFF00, match


last_move_destination = {}


def _handle_move(event):
    last_move_destination["value"] = event.move_destination
    yield ("127.0.0.1", 0)  # destination address -- unused, 0 suboperations below
    yield 0  # no suboperations to actually perform


@pytest.fixture(scope="module")
def test_scp():
    port = _free_port()
    ae = AE(ae_title="TESTSCP")
    ae.add_supported_context(Verification)
    ae.add_supported_context(PatientRootQueryRetrieveInformationModelFind)
    ae.add_supported_context(PatientRootQueryRetrieveInformationModelMove)
    handlers = [(evt.EVT_C_FIND, _handle_find), (evt.EVT_C_MOVE, _handle_move)]
    scp_thread = ae.start_server(("127.0.0.1", port), block=False, evt_handlers=handlers)
    yield port
    scp_thread.shutdown()


@pytest.fixture
def configured(test_scp, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_PACS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "LOCAL_PACS_PORT", str(test_scp))
    monkeypatch.setattr(settings, "LOCAL_PACS_AE_TITLE", "TESTSCP")
    monkeypatch.setattr(settings, "HERMES_FRONTEND_AE_TITLE", "HERMESTEST")
    monkeypatch.setattr(settings, "LOCAL_PACS_TIMEOUT_SECONDS", 5)


def test_is_configured_false_when_env_unset(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_PACS_HOST", "")
    assert cc.is_configured() is False


def test_calling_when_not_configured_raises_clearly(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_PACS_HOST", "")
    with pytest.raises(cc.ConquestNotConfigured):
        cc.echo()


def test_echo_succeeds_against_a_reachable_scp(configured):
    cc.echo()  # raises on failure


def test_echo_fails_against_an_unreachable_host(configured, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_PACS_PORT", str(_free_port()))
    with pytest.raises(cc.ConquestUnreachable):
        cc.echo()


def test_find_studies_returns_matches(configured):
    results = cc.find_studies(patient_id="ANON1")
    assert len(results) == 1
    assert results[0]["patient_id"] == "ANON1"
    assert results[0]["study_instance_uid"] == "1.2.3.4"
    assert results[0]["study_description"] == "Test study"


def test_move_study_sends_the_explicit_destination_aet(configured):
    result = cc.move_study(study_instance_uid="1.2.3.4", destination_ae="THIRDPARTY")
    assert last_move_destination["value"] == "THIRDPARTY"
    assert result.success
    assert result.completed == 0
