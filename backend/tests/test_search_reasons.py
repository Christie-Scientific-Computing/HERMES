"""
§E: search_mosaiq/search_pinnacle_db/search_proknow now each return
(found, reason) instead of a bare bool, and find_patient stitches the three
reasons into its returned dict. These are pure Importer-logic tests -- no
Postgres/Orthanc/ProKnow network calls -- so, like test_cleanup_orthanc.py,
they build Importer via object.__new__ to skip __init__ (which eagerly
connects to ProKnow/Orthanc) and fake only what each method touches.

Skips gracefully if the PinnacleExport submodule isn't checked out, since
retrieve/logic.py imports from it at module load time (see CLAUDE.md's Git
Submodule section) -- same convention as test_cleanup_orthanc.py.
"""
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

pytest.importorskip("backend.src.retrieve.PinnacleExport", reason="PinnacleExport submodule not checked out")

from backend.src.retrieve import logic as retrieve_logic
from backend.src.retrieve.logic import Importer


def make_importer(
    dicom_sources=("SRC1", "SRC2"),
    import_level="Planning",
    accepted_modalities=("CT", "RTSTRUCT", "RTPLAN", "RTDOSE"),
    plans_db=None,
    pk=None,
):
    imp = object.__new__(Importer)
    imp.dicom_sources = list(dicom_sources)
    imp.import_level = import_level
    imp.accepted_modalities = accepted_modalities
    imp.ot = MagicMock()
    imp.plans_db = plans_db
    imp.pk = pk if pk is not None else MagicMock()
    imp._started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return imp


# ---- search_mosaiq ----------------------------------------------------

class FakeModality:
    """Stands in for pyorthanc.Modality: .find(query) branches on
    query['Level'], driven entirely by canned responses/exceptions."""

    def __init__(self, study_answers=None, study_exc=None, series_by_study=None, series_exc_studies=None):
        self.study_answers = study_answers if study_answers is not None else []
        self.study_exc = study_exc
        self.series_by_study = series_by_study or {}
        self.series_exc_studies = series_exc_studies or set()

    def find(self, query):
        if query["Level"] == "Study":
            if self.study_exc:
                raise self.study_exc
            return {"answers": self.study_answers}
        study_uid = query["Query"]["StudyInstanceUID"]
        if study_uid in self.series_exc_studies:
            raise RuntimeError(f"series query blew up for {study_uid}")
        return {"answers": self.series_by_study.get(study_uid, [])}


def _patch_modality(monkeypatch, modalities: dict):
    """modalities: {src: FakeModality}"""
    monkeypatch.setattr(retrieve_logic, "Modality", lambda ot, src: modalities[src])


def test_search_mosaiq_not_found_anywhere(monkeypatch):
    imp = make_importer()
    _patch_modality(monkeypatch, {
        "SRC1": FakeModality(study_answers=[]),
        "SRC2": FakeModality(study_answers=[]),
    })

    found, reason = imp.search_mosaiq("MRN1")

    assert found is False
    assert reason == "Not found in Mosaiq"


def test_search_mosaiq_found_with_rtdose(monkeypatch):
    imp = make_importer(import_level="Planning")
    _patch_modality(monkeypatch, {
        "SRC1": FakeModality(
            study_answers=[{"StudyInstanceUID": "1.2.3"}],
            series_by_study={"1.2.3": [{"Modality": "RTDOSE"}]},
        ),
        "SRC2": FakeModality(study_answers=[]),
    })

    found, reason = imp.search_mosaiq("MRN1")

    assert found is True
    assert reason is None


def test_search_mosaiq_incomplete_planning_data(monkeypatch):
    """Studies found, but none carry an RTDOSE series -- the exact wording
    the plan calls for."""
    imp = make_importer(import_level="Planning")
    _patch_modality(monkeypatch, {
        "SRC1": FakeModality(
            study_answers=[{"StudyInstanceUID": "1.2.3"}],
            series_by_study={"1.2.3": [{"Modality": "CT"}]},
        ),
        "SRC2": FakeModality(study_answers=[]),
    })

    found, reason = imp.search_mosaiq("MRN1")

    assert found is False
    assert reason == "Incomplete planning data"


def test_search_mosaiq_outer_query_error_surfaced_when_nothing_else_found(monkeypatch):
    imp = make_importer()
    _patch_modality(monkeypatch, {
        "SRC1": FakeModality(study_exc=ConnectionError("timed out")),
        "SRC2": FakeModality(study_answers=[]),
    })

    found, reason = imp.search_mosaiq("MRN1")

    assert found is False
    assert reason == "Could not query SRC1: timed out"


def test_search_mosaiq_inner_series_query_error_does_not_propagate(monkeypatch):
    """The critical correction: the inner per-study series query used to be
    completely unguarded -- an exception there propagated uncaught out of
    search_mosaiq (and therefore find_patient), skipping Pinnacle/ProKnow
    checks entirely. It must now be caught and turned into a reason."""
    imp = make_importer()
    _patch_modality(monkeypatch, {
        "SRC1": FakeModality(
            study_answers=[{"StudyInstanceUID": "1.2.3"}],
            series_exc_studies={"1.2.3"},
        ),
        "SRC2": FakeModality(study_answers=[]),
    })

    # Must not raise.
    found, reason = imp.search_mosaiq("MRN1")

    assert found is False
    assert reason == "Could not query SRC1: series query blew up for 1.2.3"


def test_search_mosaiq_inner_query_error_on_one_study_does_not_block_another(monkeypatch):
    """The loop keeps trying other studies/sources after a per-study series
    query fails -- only the reason-tracking changed, not the "keep going"
    behavior."""
    imp = make_importer(import_level="Planning")
    _patch_modality(monkeypatch, {
        "SRC1": FakeModality(
            study_answers=[{"StudyInstanceUID": "bad"}, {"StudyInstanceUID": "good"}],
            series_by_study={"good": [{"Modality": "RTDOSE"}]},
            series_exc_studies={"bad"},
        ),
        "SRC2": FakeModality(study_answers=[]),
    })

    found, reason = imp.search_mosaiq("MRN1")

    assert found is True
    assert reason is None


def test_search_mosaiq_second_source_recovers_after_first_source_errors(monkeypatch):
    imp = make_importer(import_level="Planning")
    _patch_modality(monkeypatch, {
        "SRC1": FakeModality(study_exc=ConnectionError("down")),
        "SRC2": FakeModality(
            study_answers=[{"StudyInstanceUID": "1.2.3"}],
            series_by_study={"1.2.3": [{"Modality": "RTDOSE"}]},
        ),
    })

    found, reason = imp.search_mosaiq("MRN1")

    assert found is True
    assert reason is None


# ---- search_pinnacle_db -------------------------------------------------

class FakePlansDB:
    def __init__(self, result=None, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls = []

    def latest_status_for_patient(self, mrn, since):
        self.calls.append((mrn, since))
        if self.raise_exc:
            raise self.raise_exc
        return self.result


def _patch_pinn_db(monkeypatch, indexed_mrns):
    """Fakes sqlite3.connect(PINN_DB) with a real in-memory sqlite DB seeded
    with `entries` rows for the given mrns, mirroring the real schema.

    `retrieve_logic.sqlite3` is the exact same module object as this file's
    own `sqlite3` import (module caching in sys.modules) -- so the original
    `connect` must be captured BEFORE patching. Calling `sqlite3.connect`
    from inside `_connect` after patching would resolve to the patched
    attribute on that same shared module and recurse on itself forever.
    """
    real_connect = sqlite3.connect

    def _connect(_path):
        conn = real_connect(":memory:")
        conn.execute("CREATE TABLE entries (MedicalRecordNumber TEXT, PinnacleID TEXT, Path TEXT)")
        for mrn in indexed_mrns:
            conn.execute("INSERT INTO entries VALUES (?, ?, ?)", (str(mrn), "PID1", "/pinnacle/path"))
        conn.commit()
        return conn
    monkeypatch.setattr(retrieve_logic.sqlite3, "connect", _connect)


def test_search_pinnacle_db_not_indexed(monkeypatch):
    imp = make_importer(plans_db=FakePlansDB())
    _patch_pinn_db(monkeypatch, indexed_mrns=[])

    found, reason = imp.search_pinnacle_db("MRN1")

    assert found is False
    assert reason == "Not found in Pinnacle export index"


def test_search_pinnacle_db_indexed_no_status_row_yet_is_pending(monkeypatch):
    imp = make_importer(plans_db=FakePlansDB(result=None))
    _patch_pinn_db(monkeypatch, indexed_mrns=["MRN1"])

    found, reason = imp.search_pinnacle_db("MRN1")

    assert found is True
    assert reason == "Pinnacle reconstruction pending"


def test_search_pinnacle_db_indexed_status_shows_failure(monkeypatch):
    imp = make_importer(plans_db=FakePlansDB(result={"status": "failed", "error_message": "no RTSTRUCT"}))
    _patch_pinn_db(monkeypatch, indexed_mrns=["MRN1"])

    found, reason = imp.search_pinnacle_db("MRN1")

    assert found is True
    assert reason == "Could not reconstruct DICOM: no RTSTRUCT"


def test_search_pinnacle_db_indexed_status_shows_success(monkeypatch):
    imp = make_importer(plans_db=FakePlansDB(result={"status": "exported", "error_message": None}))
    _patch_pinn_db(monkeypatch, indexed_mrns=["MRN1"])

    found, reason = imp.search_pinnacle_db("MRN1")

    assert found is True
    assert reason is None


def test_search_pinnacle_db_status_lookup_failure_falls_back_to_pending(monkeypatch):
    """A status-lookup hiccup (e.g. transient DB error) must not make an
    otherwise-indexed patient look unfound."""
    imp = make_importer(plans_db=FakePlansDB(raise_exc=RuntimeError("pool exhausted")))
    _patch_pinn_db(monkeypatch, indexed_mrns=["MRN1"])

    found, reason = imp.search_pinnacle_db("MRN1")

    assert found is True
    assert reason == "Pinnacle reconstruction pending"


# ---- search_proknow -------------------------------------------------

def test_search_proknow_not_found(monkeypatch):
    pk = MagicMock()
    pk.patients.find.return_value = None
    imp = make_importer(pk=pk)

    found, reason = imp.search_proknow("MRN1")

    assert found is False
    assert reason == "Patient not found on ProKnow"


def test_search_proknow_found(monkeypatch):
    pk = MagicMock()
    match = MagicMock()
    pk.patients.find.return_value = match
    imp = make_importer(pk=pk)

    found, reason = imp.search_proknow("MRN1")

    assert found is True
    assert reason is None
    match.get.assert_called_once()


def test_search_proknow_connectivity_failure_is_distinguished_from_not_found(monkeypatch):
    pk = MagicMock()
    pk.patients.find.side_effect = ConnectionError("auth failed")
    imp = make_importer(pk=pk)

    found, reason = imp.search_proknow("MRN1")

    assert found is False
    assert reason == "Could not query ProKnow: auth failed"


# ---- find_patient: stitches the three (found, reason) pairs together ----

def test_find_patient_shape(monkeypatch):
    imp = make_importer()
    monkeypatch.setattr(imp, "search_mosaiq", lambda mrn: (True, None))
    monkeypatch.setattr(imp, "search_pinnacle_db", lambda mrn: (False, "Not found in Pinnacle export index"))
    monkeypatch.setattr(imp, "search_proknow", lambda mrn: (False, "Patient not found on ProKnow"))

    result = imp.find_patient("MRN1")

    assert result == {
        "in_mosaiq": True, "mosaiq_reason": None,
        "in_pinnacle": False, "pinnacle_reason": "Not found in Pinnacle export index",
        "in_proknow": False, "proknow_reason": "Patient not found on ProKnow",
    }
