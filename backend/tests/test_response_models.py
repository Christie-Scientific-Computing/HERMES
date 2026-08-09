"""
Regression coverage for §D0 of docs/safety-plan.md: the narrow `Response`
models in retrieve/endpoints.py and export/endpoints.py used to declare a
fixed, small field set. Every batch worker routes its raw result dict
through `Response(mrn=..., **res).model_dump(exclude={"mrn"})` before the
dict reaches `run_batch_job` -- and Pydantic 2 silently drops any key not
declared on the model, so new fields (added by later work items §D1-§D3/§E)
would have been silently stripped before ever reaching `events.details` or
the SSE payload. This is the exact operation exercised below.

`export.endpoints` has no import-time dependency on the PinnacleExport
submodule, so its tests always run. `retrieve.endpoints` does (it imports
`Importer` from `retrieve/logic.py`, which imports PinnacleExport at module
level) -- skip gracefully if the submodule isn't checked out, matching the
existing convention in test_cleanup_orthanc.py / test_retrieve_endpoints_errors.py.
"""
import pytest

from backend.src.export.endpoints import Response as ExportResponse

try:
    from backend.src.retrieve.endpoints import Response as ImportResponse
    _HAS_PINNACLE = True
except ImportError:
    _HAS_PINNACLE = False

requires_pinnacle = pytest.mark.skipif(
    not _HAS_PINNACLE, reason="PinnacleExport submodule not checked out"
)


# ---------------------------------------------------------------------------
# Import-side Response (backend/src/retrieve/endpoints.py)
# ---------------------------------------------------------------------------

@requires_pinnacle
def test_import_response_round_trips_all_new_fields():
    """
    The exact operation _import_worker performs: Response(mrn=..., **res)
    then .model_dump(exclude={"mrn"}). Before the §D0 fix, mosaiq_reason/
    pinnacle_reason/proknow_reason/imported/study_count/study_uids would
    have been silently dropped here.
    """
    res = {
        "status": "success",
        "in_mosaiq": True,
        "in_pinnacle": False,
        "in_proknow": True,
        "mosaiq_reason": None,
        "pinnacle_reason": "Not found in Pinnacle export index",
        "proknow_reason": None,
        "imported": True,
        "study_count": 2,
        "study_uids": ["1.2.3", "1.2.4"],
    }
    dumped = ImportResponse(mrn="123", **res).model_dump(exclude={"mrn"})

    assert dumped["imported"] is True
    assert dumped["study_count"] == 2
    assert dumped["study_uids"] == ["1.2.3", "1.2.4"]
    assert dumped["mosaiq_reason"] is None
    assert dumped["pinnacle_reason"] == "Not found in Pinnacle export index"
    assert dumped["proknow_reason"] is None
    assert dumped["in_mosaiq"] is True
    assert dumped["in_pinnacle"] is False
    assert dumped["in_proknow"] is True
    assert dumped["status"] == "success"
    assert "mrn" not in dumped


@requires_pinnacle
def test_import_response_defaults_to_none_when_new_fields_unset():
    """Backward compatibility: existing callers that only pass the original
    fields shouldn't break, and unset new fields should default to None."""
    res = {"status": "success", "in_mosaiq": True, "in_pinnacle": False, "in_proknow": False}
    response = ImportResponse(mrn="123", **res)

    assert response.mosaiq_reason is None
    assert response.pinnacle_reason is None
    assert response.proknow_reason is None
    assert response.imported is None
    assert response.study_count is None
    assert response.study_uids is None

    dumped = response.model_dump(exclude={"mrn"})
    assert dumped["in_mosaiq"] is True
    assert dumped["imported"] is None
    assert dumped["study_uids"] is None

    # exclude_none=True is what _import_worker actually uses when building
    # the dict that feeds events.details / the SSE payload -- confirms the
    # new None-valued fields don't pad every event until D1/D2/D3/E populate
    # them.
    dumped_clean = response.model_dump(exclude={"mrn"}, exclude_none=True)
    assert "mosaiq_reason" not in dumped_clean
    assert "imported" not in dumped_clean
    assert "study_uids" not in dumped_clean
    assert dumped_clean["in_mosaiq"] is True
    assert dumped_clean["status"] == "success"


@requires_pinnacle
def test_import_response_still_requires_mrn():
    with pytest.raises(Exception):
        ImportResponse(status="success")


# ---------------------------------------------------------------------------
# Export-side Response (backend/src/export/endpoints.py)
# ---------------------------------------------------------------------------

def test_export_response_round_trips_all_new_fields():
    """
    The exact operation _dicom_move_worker/_proknow_worker perform:
    Response(mrn=..., **res) then .model_dump(exclude={"mrn"}). Before the
    §D0 fix, series_count/instance_count/study_uids/series_uids/checksums/
    destination/destination_type/submitted_by would have been silently
    dropped here.
    """
    res = {
        "status": "Success",
        "series_count": 3,
        "instance_count": 150,
        "study_uids": ["1.2.3"],
        "series_uids": ["1.2.3.1", "1.2.3.2", "1.2.3.3"],
        "checksums": {"1.2.3.1": "abc123", "1.2.3.2": "def456"},
        "destination": "PACS_AE",
        "destination_type": "dicom_modality",
        "submitted_by": "jdoe",
    }
    dumped = ExportResponse(mrn="123", **res).model_dump(exclude={"mrn"})

    assert dumped["series_count"] == 3
    assert dumped["instance_count"] == 150
    assert dumped["study_uids"] == ["1.2.3"]
    assert dumped["series_uids"] == ["1.2.3.1", "1.2.3.2", "1.2.3.3"]
    assert dumped["checksums"] == {"1.2.3.1": "abc123", "1.2.3.2": "def456"}
    assert dumped["destination"] == "PACS_AE"
    assert dumped["destination_type"] == "dicom_modality"
    assert dumped["submitted_by"] == "jdoe"
    assert dumped["status"] == "Success"
    assert "mrn" not in dumped


def test_export_response_defaults_to_none_when_new_fields_unset():
    """Backward compatibility: today's workers only return {'status': ...};
    unset new fields should default to None and existing callers shouldn't break."""
    res = {"status": "Success"}
    response = ExportResponse(mrn="123", **res)

    assert response.series_count is None
    assert response.instance_count is None
    assert response.study_uids is None
    assert response.series_uids is None
    assert response.checksums is None
    assert response.destination is None
    assert response.destination_type is None
    assert response.submitted_by is None

    dumped = response.model_dump(exclude={"mrn"})
    assert dumped["status"] == "Success"
    assert dumped["series_count"] is None

    # exclude_none=True is what _dicom_move_worker/_proknow_worker actually
    # use -- confirms the new None-valued fields don't pad every event until
    # D2 populates them.
    dumped_clean = response.model_dump(exclude={"mrn"}, exclude_none=True)
    assert dumped_clean == {"status": "Success"}


def test_export_response_still_requires_mrn():
    with pytest.raises(Exception):
        ExportResponse(status="Success")
