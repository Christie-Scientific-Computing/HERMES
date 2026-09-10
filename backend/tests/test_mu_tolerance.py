"""
Unit tests for F001's MU-tolerance resolution:
- _resolve_mu_tolerance_default's own env-var parsing (backend/src/retrieve/logic.py).
- Importer.__init__'s "explicit value wins, else the module default" rule --
  exercised via the REAL __init__, with only _init_connections (ProKnow/
  Orthanc) monkeypatched to a no-op; PlansDB() is cheap/lazy so it's left
  real, same as test_cleanup_orthanc.py's own reasoning for what needs
  faking and what doesn't.
- import_from_pinnacle's payload carries whatever self.mu_tolerance
  resolved to.
"""
import pytest

pytest.importorskip("backend.src.retrieve.PinnacleExport", reason="PinnacleExport submodule not checked out")

from backend.src.retrieve import logic as retrieve_logic
from backend.src.retrieve.logic import Importer, _resolve_mu_tolerance_default, resolve_mu_tolerance


@pytest.fixture(autouse=True)
def _no_real_connections(monkeypatch):
    monkeypatch.setattr(Importer, "_init_connections", lambda self: None)


@pytest.mark.parametrize("raw, expected", [(None, None), ("", None), ("0.5", 0.5), ("2", 2.0)])
def test_resolve_mu_tolerance_default(raw, expected):
    assert _resolve_mu_tolerance_default(raw) == expected


def test_resolve_mu_tolerance_prefers_the_explicit_value(monkeypatch):
    monkeypatch.setattr(retrieve_logic, "MU_TOLERANCE_DEFAULT", 1.0)
    assert resolve_mu_tolerance(0.5) == 0.5


def test_resolve_mu_tolerance_falls_back_to_the_module_default(monkeypatch):
    monkeypatch.setattr(retrieve_logic, "MU_TOLERANCE_DEFAULT", 1.0)
    assert resolve_mu_tolerance(None) == 1.0


class TestImporterMuToleranceResolution:
    def test_explicit_value_is_used_even_when_a_default_exists(self, monkeypatch):
        monkeypatch.setattr(retrieve_logic, "MU_TOLERANCE_DEFAULT", 1.0)
        imp = Importer(mu_tolerance=0.5)
        assert imp.mu_tolerance == 0.5

    def test_none_falls_back_to_the_module_default(self, monkeypatch):
        monkeypatch.setattr(retrieve_logic, "MU_TOLERANCE_DEFAULT", 1.0)
        imp = Importer(mu_tolerance=None)
        assert imp.mu_tolerance == 1.0

    def test_none_stays_none_when_no_default_is_configured(self, monkeypatch):
        monkeypatch.setattr(retrieve_logic, "MU_TOLERANCE_DEFAULT", None)
        imp = Importer(mu_tolerance=None)
        assert imp.mu_tolerance is None


def test_import_from_pinnacle_payload_carries_mu_tolerance(monkeypatch):
    imp = Importer(mu_tolerance=0.5)
    monkeypatch.setattr(imp, "get_pinn_export_requests", lambda mrn: ["fake-request"])
    captured = {}
    monkeypatch.setattr(retrieve_logic, "pinn_entry", lambda payload: captured.update(payload))

    imp.import_from_pinnacle("R1")

    assert captured["mu_tolerance"] == 0.5
    assert captured["requests"] == ["fake-request"]


def test_import_from_pinnacle_payload_carries_none_when_unresolved(monkeypatch):
    monkeypatch.setattr(retrieve_logic, "MU_TOLERANCE_DEFAULT", None)
    imp = Importer(mu_tolerance=None)
    monkeypatch.setattr(imp, "get_pinn_export_requests", lambda mrn: [])
    captured = {}
    monkeypatch.setattr(retrieve_logic, "pinn_entry", lambda payload: captured.update(payload))

    imp.import_from_pinnacle("R1")

    assert captured["mu_tolerance"] is None
