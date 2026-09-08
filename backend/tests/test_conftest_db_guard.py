"""
Self-check for conftest.py's pytest_configure DATABASE_URL guard -- the
code-level fix for the 2026-09-07 incident where tests run against this
repo's real (non-throwaway) DATABASE_URL destroyed pinnacle_index and
pinnacle_export twice in one session. Loads conftest.py directly via
importlib rather than as a package import, since backend/tests/ has no
__init__.py.
"""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_hermes_conftest_under_test", Path(__file__).parent / "conftest.py"
)
_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conftest)


def test_non_loopback_host_is_refused(monkeypatch):
    monkeypatch.setattr(_conftest, "TEST_DATABASE_URL", "postgresql://u:p@192.168.117.5:5432/db")
    monkeypatch.delenv("HERMES_TESTS_ALLOW_REMOTE_DB", raising=False)
    with pytest.raises(pytest.UsageError):
        _conftest.pytest_configure(config=None)


def test_loopback_host_is_allowed(monkeypatch):
    monkeypatch.setattr(_conftest, "TEST_DATABASE_URL", "postgresql://u:p@localhost:55432/db")
    monkeypatch.delenv("HERMES_TESTS_ALLOW_REMOTE_DB", raising=False)
    _conftest.pytest_configure(config=None)  # must not raise


def test_explicit_override_allows_non_loopback_host(monkeypatch):
    monkeypatch.setattr(_conftest, "TEST_DATABASE_URL", "postgresql://u:p@192.168.117.5:5432/db")
    monkeypatch.setenv("HERMES_TESTS_ALLOW_REMOTE_DB", "1")
    _conftest.pytest_configure(config=None)  # must not raise
