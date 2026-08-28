"""
Tests for ANON_CONFIG: the XML-config-file + Fernet-encrypted-credential
sourcing mechanism reintroduced from gateway/anon.py's AnonDatabase (git
history 4ecab3c0e9eea5091ca36b434ffee5c266bdbfd7) into this module's
_connection_kwargs(). ANON_CONFIG stays unset here except where a test
explicitly monkeypatches it, so these tests don't affect any other test
file's plain-ANON_DB_* expectations of the shared `anon` module object.
"""
import os

import pytest
from cryptography.fernet import Fernet

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"

from backend.src.identity import anon  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(anon, "_pool", None)
    yield
    monkeypatch.setattr(anon, "_pool", None)


def _write_config(tmp_path, key, user="cfguser", password="cfgpass",
                   host="10.0.0.5", port="6543", dbname="cfgdb"):
    fernet = Fernet(key)
    xml = f"""<config>
      <keyDataBase>
        <dataBaseServer>unused-hostname</dataBaseServer>
        <dataBaseIP>{host}</dataBaseIP>
        <dataBaseName>{dbname}</dataBaseName>
        <dataBasePort>{port}</dataBasePort>
        <dataBaseUserName>{fernet.encrypt(user.encode()).decode()}</dataBaseUserName>
        <dataBasePassword>{fernet.encrypt(password.encode()).decode()}</dataBasePassword>
      </keyDataBase>
    </config>"""
    path = tmp_path / "anon_config.xml"
    path.write_text(xml)
    return str(path)


def test_is_configured_true_via_anon_config_alone(monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_HOST", None)
    monkeypatch.setattr(anon, "ANON_CONFIG", "/some/path.xml")
    assert anon.is_configured() is True


def test_is_configured_false_when_neither_set(monkeypatch):
    monkeypatch.setattr(anon, "ANON_DB_HOST", None)
    monkeypatch.setattr(anon, "ANON_CONFIG", None)
    assert anon.is_configured() is False


def test_connection_kwargs_from_config_file(monkeypatch, tmp_path):
    path = _write_config(tmp_path, anon.ANON_CONFIG_KEY)
    monkeypatch.setattr(anon, "ANON_CONFIG", path)
    kwargs = anon._connection_kwargs()
    assert kwargs["host"] == "10.0.0.5"
    assert kwargs["port"] == 6543
    assert kwargs["dbname"] == "cfgdb"
    assert kwargs["user"] == "cfguser"
    assert kwargs["password"] == "cfgpass"
    assert kwargs["connect_timeout"] == 5


def test_connection_kwargs_config_file_takes_precedence_over_plain_env(monkeypatch, tmp_path):
    path = _write_config(tmp_path, anon.ANON_CONFIG_KEY, host="10.0.0.9")
    monkeypatch.setattr(anon, "ANON_CONFIG", path)
    # plain ANON_DB_* still set from module import above -- must be ignored
    kwargs = anon._connection_kwargs()
    assert kwargs["host"] == "10.0.0.9"
    assert kwargs["host"] != anon.ANON_DB_HOST


def test_connection_kwargs_respects_custom_config_key(monkeypatch, tmp_path):
    custom_key = Fernet.generate_key().decode()
    path = _write_config(tmp_path, custom_key)
    monkeypatch.setattr(anon, "ANON_CONFIG", path)
    monkeypatch.setattr(anon, "ANON_CONFIG_KEY", custom_key)
    assert anon._connection_kwargs()["user"] == "cfguser"


def test_connection_kwargs_wrong_key_raises(monkeypatch, tmp_path):
    path = _write_config(tmp_path, Fernet.generate_key().decode())
    monkeypatch.setattr(anon, "ANON_CONFIG", path)
    # ANON_CONFIG_KEY left at the module default, which doesn't match the
    # key the file above was actually encrypted with.
    with pytest.raises(Exception):  # cryptography.fernet.InvalidToken
        anon._connection_kwargs()


def test_connection_kwargs_ssl_opts_still_apply_with_config_file(monkeypatch, tmp_path):
    path = _write_config(tmp_path, anon.ANON_CONFIG_KEY)
    monkeypatch.setattr(anon, "ANON_CONFIG", path)
    monkeypatch.setattr(anon, "ANON_DB_SSLMODE", "require")
    monkeypatch.setattr(anon, "ANON_DB_SSLROOTCERT", None)
    kwargs = anon._connection_kwargs()
    assert kwargs["sslmode"] == "require"
    assert kwargs["host"] == "10.0.0.5"


def test_missing_config_file_wrapped_as_anon_service_error(monkeypatch):
    """Exercises the real integration point: _query()'s existing
    except-Exception-around-_get_pool() wraps a bad ANON_CONFIG without any
    new try/except added to this module."""
    monkeypatch.setattr(anon, "ANON_CONFIG", "/nonexistent/anon_config.xml")
    with pytest.raises(anon.AnonServiceError):
        anon.lookup_real_ids(["1001"])
