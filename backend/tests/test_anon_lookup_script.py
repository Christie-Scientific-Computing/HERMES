import os

import pytest

os.environ["ANON_DB_HOST"] = "localhost"
os.environ["ANON_DB_PORT"] = "55433"
os.environ["ANON_DB_NAME"] = "anon_test"
os.environ["ANON_DB_USER"] = "postgres"
os.environ["ANON_DB_PASS"] = "test"
os.environ["ANON_CONFIG"] = ""  # a dev .env's config file would otherwise override the test DB

from backend.scripts.anon_lookup import main  # noqa: E402
from backend.src.identity import anon  # noqa: E402


def run(tmp_path, content, *flags):
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    src.write_text(content, encoding="utf-8")
    return main([str(src), str(dst), *flags]), dst


def test_to_real(tmp_path):
    code, dst = run(tmp_path, "anon_id,note\n1001,a\n1002,b\n", "--to-real", "--column", "anon_id")
    assert code == 0
    assert dst.read_text().splitlines() == ["anon_id,note,patient_id", "1001,a,500123", "1002,b,500456"]


def test_to_anon_default_column(tmp_path):
    code, dst = run(tmp_path, "mrn\n500123\n500456\n", "--to-anon")
    assert code == 0
    assert dst.read_text().splitlines() == ["mrn,anon_id", "500123,1001", "500456,1002"]


def test_unmapped_to_real_blank_and_reported(tmp_path, capsys):
    # 9999 exists only under key_type_id=2 so must count as unmapped too
    code, dst = run(tmp_path, "id\n1001\n424242\nnot-a-number\n9999\n\n", "--to-real")
    assert code == 1
    assert dst.read_text().splitlines() == ["id,patient_id", "1001,500123", "424242,", "not-a-number,", "9999,", ","]
    err = capsys.readouterr().err
    assert "1 translated, 4 unmapped" in err and "2, 3, 4, 5" in err
    assert "424242" not in err


def test_out_of_range_id_is_unmapped_not_fatal(tmp_path):
    code, dst = run(tmp_path, "id\n1001\n99999999999999999999\n", "--to-real")
    assert code == 1
    assert dst.read_text().splitlines() == ["id,patient_id", "1001,500123", "99999999999999999999,"]


def test_unmapped_to_anon_is_blank_not_placeholder(tmp_path):
    code, dst = run(tmp_path, "id\n000000\n", "--to-anon")
    assert code == 1
    assert dst.read_text().splitlines() == ["id,anon_id", "000000,"]


def test_whitespace_bom_and_repeats(tmp_path):
    code, dst = run(tmp_path, "﻿id\n 1001\n1001 \n", "--to-real")
    assert code == 0
    assert dst.read_text().splitlines() == ["id,patient_id", " 1001,500123", "1001 ,500123"]


def test_header_only(tmp_path):
    code, dst = run(tmp_path, "id\n", "--to-real")
    assert code == 0
    assert dst.read_text().splitlines() == ["id,patient_id"]


def test_missing_column_writes_nothing(tmp_path, capsys):
    code, dst = run(tmp_path, "id,note\n1001,a\n", "--to-real", "--column", "nope")
    assert code == 2 and not dst.exists()
    assert "id, note" in capsys.readouterr().err


def test_not_configured_no_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(anon, "is_configured", lambda: False)
    code, dst = run(tmp_path, "id\n1001\n", "--to-real")
    assert code == 2 and not dst.exists()


def test_db_unreachable_writes_nothing_and_leaks_no_ids(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise anon.AnonServiceError("Cannot reach anonymisation DB: down")

    monkeypatch.setattr(anon, "_query", boom)
    code, dst = run(tmp_path, "id\n1001\n", "--to-real")
    assert code == 2 and not dst.exists()
    assert "1001" not in capsys.readouterr().err


def test_output_equals_input_refused(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("id\n1001\n")
    assert main([str(src), str(src), "--to-real"]) == 2
    assert src.read_text() == "id\n1001\n"


def test_direction_flag_required(tmp_path):
    with pytest.raises(SystemExit) as e:
        run(tmp_path, "id\n1001\n")
    assert e.value.code == 2
