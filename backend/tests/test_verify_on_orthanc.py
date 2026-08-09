"""
Tests for §D3 of docs/safety-plan.md: Importer.verify_on_orthanc, and its
wiring into Importer.handle_patient.

verify_on_orthanc is "ground truth" -- what Orthanc actually holds for a
patient right now, independent of what find_patient predicted beforehand.
handle_patient must call it *after* import_patient (which itself runs
_cleanup_orthanc internally) has finished, so the result reflects what
survived cleanup, not what was found pre-cleanup, and must merge its result
into the dict it returns alongside in_mosaiq/in_pinnacle/in_proknow.

Uses a faked Orthanc client / find_studies -- no live Orthanc needed. Follows
test_cleanup_orthanc.py's exact pattern: Importer is built via
object.__new__ to skip __init__ (which eagerly connects to ProKnow/Orthanc),
and only the attributes each method actually touches are set manually.
"""
from unittest.mock import MagicMock

import pytest

# retrieve/logic.py imports the PinnacleExport git submodule, which may not
# be checked out (see CLAUDE.md's Git Submodule section) -- skip gracefully
# rather than blocking the rest of the suite when it isn't available.
pytest.importorskip("backend.src.retrieve.PinnacleExport", reason="PinnacleExport submodule not checked out")

from backend.src.retrieve import logic as retrieve_logic
from backend.src.retrieve.logic import Importer


class FakeStudy:
    def __init__(self, uid):
        self.main_dicom_tags = {"StudyInstanceUID": uid}


def make_importer():
    imp = object.__new__(Importer)
    imp.ot = MagicMock()
    return imp


def _patch_studies(monkeypatch, studies_list):
    monkeypatch.setattr(retrieve_logic, "find_studies", lambda client, query: studies_list)


# ---------------------------------------------------------------------------
# verify_on_orthanc itself
# ---------------------------------------------------------------------------

def test_verify_on_orthanc_reports_studies_found(monkeypatch):
    studies = [FakeStudy("1.2.3"), FakeStudy("1.2.4")]
    _patch_studies(monkeypatch, studies)

    imp = make_importer()
    result = imp.verify_on_orthanc("MRN1")

    assert result == {
        "imported": True,
        "study_count": 2,
        "study_uids": ["1.2.3", "1.2.4"],
    }


def test_verify_on_orthanc_zero_studies_edge_case(monkeypatch):
    """Nothing survived (or was ever imported) -- imported must be False,
    not just an empty/falsy study_count, and study_uids must be []."""
    _patch_studies(monkeypatch, [])

    imp = make_importer()
    result = imp.verify_on_orthanc("MRN1")

    assert result == {
        "imported": False,
        "study_count": 0,
        "study_uids": [],
    }


def test_verify_on_orthanc_queries_by_patient_id(monkeypatch):
    """Confirms it queries Orthanc directly by PatientID (find_studies),
    not some other filter, and passes the client through -- mirroring
    _cleanup_orthanc's own find_series(client=self.ot, ...) call pattern."""
    captured = {}

    def fake_find_studies(client, query):
        captured["client"] = client
        captured["query"] = query
        return []

    monkeypatch.setattr(retrieve_logic, "find_studies", fake_find_studies)

    imp = make_importer()
    imp.verify_on_orthanc(12345)

    assert captured["client"] is imp.ot
    assert captured["query"] == {"PatientID": "12345"}


# ---------------------------------------------------------------------------
# handle_patient's wiring: verify_on_orthanc runs after import_patient
# (i.e. after _cleanup_orthanc), and its result is merged into the return
# ---------------------------------------------------------------------------

def test_handle_patient_merges_verification_result(monkeypatch):
    imp = make_importer()

    locations = {"in_mosaiq": True, "in_pinnacle": False, "in_proknow": True}
    monkeypatch.setattr(imp, "find_patient", lambda mrn: dict(locations))
    monkeypatch.setattr(imp, "import_patient", lambda mrn, locs: None)
    monkeypatch.setattr(
        imp, "verify_on_orthanc",
        lambda mrn: {"imported": True, "study_count": 3, "study_uids": ["1.2.3", "1.2.4", "1.2.5"]},
    )

    result = imp.handle_patient("MRN1")

    assert result == {
        "status": "success",
        "in_mosaiq": True,
        "in_pinnacle": False,
        "in_proknow": True,
        "imported": True,
        "study_count": 3,
        "study_uids": ["1.2.3", "1.2.4", "1.2.5"],
    }


def test_handle_patient_verification_reflects_zero_studies(monkeypatch):
    """Edge case end-to-end through handle_patient: even if find_patient
    predicted the patient was findable, if nothing actually landed on
    Orthanc (e.g. cleanup deleted everything), imported must come back
    False with an empty study_uids list."""
    imp = make_importer()

    monkeypatch.setattr(imp, "find_patient", lambda mrn: {"in_mosaiq": True, "in_pinnacle": False, "in_proknow": False})
    monkeypatch.setattr(imp, "import_patient", lambda mrn, locs: None)
    _patch_studies(monkeypatch, [])  # real verify_on_orthanc runs, backed by faked find_studies

    result = imp.handle_patient("MRN1")

    assert result["imported"] is False
    assert result["study_count"] == 0
    assert result["study_uids"] == []
    assert result["status"] == "success"


def test_handle_patient_calls_verify_after_import(monkeypatch):
    """The whole point of D3: verification must reflect what survived
    _cleanup_orthanc, so import_patient (which runs cleanup internally)
    must complete before verify_on_orthanc runs."""
    imp = make_importer()
    call_order = []

    monkeypatch.setattr(imp, "find_patient", lambda mrn: {"in_mosaiq": True, "in_pinnacle": False, "in_proknow": False})

    def fake_import_patient(mrn, locs):
        call_order.append("import_patient")

    def fake_verify(mrn):
        call_order.append("verify_on_orthanc")
        return {"imported": True, "study_count": 1, "study_uids": ["1.2.3"]}

    monkeypatch.setattr(imp, "import_patient", fake_import_patient)
    monkeypatch.setattr(imp, "verify_on_orthanc", fake_verify)

    imp.handle_patient("MRN1")

    assert call_order == ["import_patient", "verify_on_orthanc"]
