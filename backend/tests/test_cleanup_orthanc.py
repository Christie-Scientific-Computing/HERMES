"""
Characterization tests for Importer._cleanup_orthanc -- real clinical
dedup/pruning logic. This refactor (SQL injection fix, hardcoded-destination
fix in the same file) is not supposed to touch this method's behavior at
all; these tests exist to catch any accidental drift.

Uses a faked Orthanc client / find_series -- no live Orthanc needed.
Importer is built via object.__new__ to skip __init__ (which eagerly
connects to ProKnow/Orthanc), and only the attributes _cleanup_orthanc
actually touches are set manually.
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
    def __init__(self, uid, identifier):
        self.main_dicom_tags = {"StudyInstanceUID": uid}
        self.identifier = identifier


class FakeSeries:
    def __init__(self, identifier, study, tags):
        self.identifier = identifier
        self.parent_study = study
        self.main_dicom_tags = tags


def make_importer(import_level="Planning", get_series_id_instances=None):
    imp = object.__new__(Importer)
    imp.import_level = import_level
    imp.accepted_modalities = ("CT", "RTSTRUCT", "RTPLAN", "RTDOSE")
    imp.ot = MagicMock()
    imp.ot.get_series_id_instances = get_series_id_instances or (lambda series_id: [MagicMock()])
    return imp


@pytest.fixture
def deletes(monkeypatch):
    """Capture every orthanc_delete call instead of hitting a real Orthanc."""
    calls = []
    monkeypatch.setattr(Importer, "orthanc_delete", staticmethod(lambda url: calls.append(url)))
    return calls


def _patch_series(monkeypatch, series_list):
    monkeypatch.setattr(retrieve_logic, "find_series", lambda client, query: series_list)


def test_study_missing_rtdose_in_planning_mode_deletes_whole_study(monkeypatch, deletes):
    study = FakeStudy("1.2.3", "study-orthanc-id")
    series_list = [
        FakeSeries("s1", study, {"Modality": "CT", "Manufacturer": "SIEMENS", "StationName": "st1"}),
        FakeSeries("s2", study, {"Modality": "RTSTRUCT", "Manufacturer": "", "StationName": "st1"}),
    ]
    _patch_series(monkeypatch, series_list)

    imp = make_importer(import_level="Planning")
    imp._cleanup_orthanc("MRN1")

    assert deletes == ["/studies/study-orthanc-id"]


def test_elekta_cbct_deleted_in_planning_mode(monkeypatch, deletes):
    study = FakeStudy("1.2.3", "study-orthanc-id")
    series_list = [
        FakeSeries("rtdose", study, {"Modality": "RTDOSE", "Manufacturer": "", "StationName": "st1"}),
        FakeSeries("cbct", study, {"Modality": "CT", "Manufacturer": "ELEKTA", "StationName": "st1"}),
    ]
    _patch_series(monkeypatch, series_list)

    imp = make_importer(import_level="Planning")
    imp._cleanup_orthanc("MRN1")

    assert deletes == ["/series/cbct"]


def test_modality_outside_accepted_list_is_deleted(monkeypatch, deletes):
    study = FakeStudy("1.2.3", "study-orthanc-id")
    series_list = [
        FakeSeries("rtdose", study, {"Modality": "RTDOSE", "Manufacturer": "", "StationName": "st1"}),
        FakeSeries("mr", study, {"Modality": "MR", "Manufacturer": "", "StationName": "st1"}),
    ]
    _patch_series(monkeypatch, series_list)

    imp = make_importer(import_level="Planning")  # MR not in Planning's accepted_modalities
    imp._cleanup_orthanc("MRN1")

    assert deletes == ["/series/mr"]


def test_rtstruct_station_mismatch_with_excess_count_deleted(monkeypatch, deletes):
    study = FakeStudy("1.2.3", "study-orthanc-id")
    series_list = [
        FakeSeries("rtdose", study, {"Modality": "RTDOSE", "Manufacturer": "", "StationName": "dose-station"}),
        FakeSeries("struct-good", study, {"Modality": "RTSTRUCT", "Manufacturer": "", "StationName": "dose-station"}),
        FakeSeries("struct-mosaiq", study, {"Modality": "RTSTRUCT", "Manufacturer": "", "StationName": "other-station"}),
    ]
    _patch_series(monkeypatch, series_list)

    imp = make_importer(import_level="Planning")
    imp._cleanup_orthanc("MRN1")

    # struct_per_modality(RTSTRUCT)=2 > RTDOSE=1, and struct-mosaiq's station
    # doesn't match the dose's station -- only that one gets deleted.
    assert deletes == ["/series/struct-mosaiq"]


def test_rtplan_station_mismatch_with_excess_count_deleted(monkeypatch, deletes):
    study = FakeStudy("1.2.3", "study-orthanc-id")
    series_list = [
        FakeSeries("rtdose", study, {"Modality": "RTDOSE", "Manufacturer": "", "StationName": "dose-station"}),
        FakeSeries("plan-good", study, {"Modality": "RTPLAN", "Manufacturer": "", "StationName": "dose-station"}),
        FakeSeries("plan-mosaiq", study, {"Modality": "RTPLAN", "Manufacturer": "", "StationName": "other-station"}),
    ]
    _patch_series(monkeypatch, series_list)

    imp = make_importer(import_level="Planning")
    imp._cleanup_orthanc("MRN1")

    assert deletes == ["/series/plan-mosaiq"]


def test_duplicate_rtstruct_instances_are_logged_not_deleted(monkeypatch, deletes):
    """Confirms the known #TODO dead code path stays dead: duplicate ProKnow
    RTSTRUCT instances are only logged, never deleted, by this branch."""
    study = FakeStudy("1.2.3", "study-orthanc-id")
    series_list = [
        FakeSeries("rtdose", study, {"Modality": "RTDOSE", "Manufacturer": "", "StationName": "dose-station"}),
        FakeSeries("struct-dup", study, {"Modality": "RTSTRUCT", "Manufacturer": "", "StationName": "dose-station"}),
    ]
    _patch_series(monkeypatch, series_list)

    # 2 instances under the one RTSTRUCT series -- triggers the "duplicate" log branch
    imp = make_importer(
        import_level="Planning",
        get_series_id_instances=lambda series_id: [MagicMock(), MagicMock()],
    )
    imp._cleanup_orthanc("MRN1")

    # RTSTRUCT count (1) does not exceed RTDOSE count (1), so the
    # station-mismatch deletion rule doesn't fire either -- nothing deleted.
    assert deletes == []


def test_no_series_found_locally_is_a_noop(monkeypatch, deletes):
    _patch_series(monkeypatch, [])
    imp = make_importer(import_level="Planning")
    imp._cleanup_orthanc("MRN1")
    assert deletes == []


def test_images_only_mode_keeps_studies_without_rtdose(monkeypatch, deletes):
    """Images-only import level shouldn't apply the Planning-mode RTDOSE rule."""
    study = FakeStudy("1.2.3", "study-orthanc-id")
    series_list = [
        FakeSeries("ct", study, {"Modality": "CT", "Manufacturer": "SIEMENS", "StationName": "st1"}),
    ]
    _patch_series(monkeypatch, series_list)

    imp = make_importer(import_level="Images")
    imp.accepted_modalities = ("CT", "MR", "REG")
    imp._cleanup_orthanc("MRN1")

    assert deletes == []
