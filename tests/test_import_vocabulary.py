import csv
import json
import os
from pathlib import Path
import subprocess

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from topik_question_lab.maintenance import import_vocabulary as vocabulary


SAMPLE = [
    ("단어", "명사", 'word, "term"', "từ", "単語", "새 단어를 배워요.\n복습해요.", "I learn a word.", "3"),
    ("표현", "", "expression", "biểu hiện", "表現", "표현을 익혀요.", "I learn an expression.", "6"),
    ("단어", "동사", "synthetic verb", "từ", "単語", "테스트 예문", "A test example.", "4"),
]


def write_csv(path, rows=SAMPLE, headers=vocabulary.HEADERS):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def test_csv_preserves_unicode_quotes_newlines_and_empty_pos(tmp_path):
    rows = vocabulary.read_vocabulary(write_csv(tmp_path / "words.csv"))
    assert rows == [(*row[:-1], int(row[-1])) for row in SAMPLE]


@pytest.mark.parametrize("rows,headers", [
    (SAMPLE, ("unexpected",)),
    ([], vocabulary.HEADERS),
    ([SAMPLE[0], SAMPLE[0]], vocabulary.HEADERS),
    ([SAMPLE[0][:-1]], vocabulary.HEADERS),
    ([(*SAMPLE[0], "extra")], vocabulary.HEADERS),
    ([("", *SAMPLE[0][1:])], vocabulary.HEADERS),
    ([("   ", *SAMPLE[0][1:])], vocabulary.HEADERS),
    ([(*SAMPLE[0][:-1], "2")], vocabulary.HEADERS),
    ([(*SAMPLE[0][:-1], "three")], vocabulary.HEADERS),
    ([("bad\x00word", *SAMPLE[0][1:])], vocabulary.HEADERS),
])
def test_invalid_csv_rejected_before_database_access(tmp_path, monkeypatch, rows, headers):
    path = write_csv(tmp_path / "words.csv", rows, headers)
    monkeypatch.setattr(vocabulary, "_local_config", lambda *a: pytest.fail("must validate first"))
    with pytest.raises(ValueError):
        vocabulary.import_vocabulary(tmp_path, "unused", path)


@pytest.mark.parametrize("data", [b"\xff", b'"unterminated'])
def test_unreadable_csv_is_sanitized(tmp_path, data):
    path = tmp_path / "words.csv"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="읽을 수 없습니다"):
        vocabulary.read_vocabulary(path)


def test_check_only_never_connects(tmp_path, monkeypatch, capsys):
    path = write_csv(tmp_path / "words.csv")
    monkeypatch.setattr(vocabulary.psycopg, "connect", lambda *a, **kw: pytest.fail("must not connect"))
    assert vocabulary.main(["--csv", str(path), "--check-only"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "csv_rows": 3, "levels": {"3": 1, "4": 1, "6": 1}, "empty_part_of_speech": 1,
    }


@pytest.fixture
def local_bank(postgres_database_factory, tmp_path, monkeypatch):
    binaries = os.getenv("TEST_PG_BIN_DIR")
    if not binaries:
        pytest.skip("TEST_PG_BIN_DIR supplies local backup tools")
    url = postgres_database_factory()
    with psycopg.connect(url) as connection:
        data_directory = connection.execute("SHOW data_directory").fetchone()[0]
        connection.execute("CREATE SCHEMA topik_bank")
        connection.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY)")
        for path in sorted(vocabulary.MIGRATION_DIR.glob("*.sql")):
            if path.stem >= vocabulary.VERSION:
                break
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO topik_bank.schema_migrations VALUES (%s)", (path.stem,))
    config = {"bin_directory": binaries, "data_directory": data_directory}
    monkeypatch.setattr(vocabulary, "_local_config", lambda *a: config)
    return url, tmp_path, write_csv(tmp_path / "words.csv"), config


def test_import_retry_update_and_backup_restore(local_bank, postgres_database_factory):
    url, root, path, config = local_bank
    report = vocabulary.import_vocabulary(root, url, path)
    assert (report["created"], report["updated"], report["unchanged"], report["total_rows"]) == (3, 0, 0, 3)
    assert report["verified"] and report["empty_part_of_speech"] == 1
    assert report["levels"] == {3: 1, 4: 1, 6: 1}
    with psycopg.connect(url) as connection:
        ids = connection.execute("SELECT vocabulary_id FROM topik_bank.vocabulary ORDER BY vocabulary_id").fetchall()
    retry = vocabulary.import_vocabulary(root, url, path)
    assert (retry["created"], retry["updated"], retry["unchanged"]) == (0, 0, 3)
    changed = (*SAMPLE[0][:2], "updated meaning", *SAMPLE[0][3:-1], "5")
    write_csv(path, [changed])
    updated = vocabulary.import_vocabulary(root, url, path)
    assert (updated["created"], updated["updated"], updated["unchanged"], updated["total_rows"]) == (0, 1, 0, 3)
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT vocabulary_id FROM topik_bank.vocabulary ORDER BY vocabulary_id").fetchall() == ids
        stored = vocabulary._stored_rows(connection)
        assert stored[("단어", "명사")] == (*changed[:-1], 5)
        assert stored[("표현", "")] == (*SAMPLE[1][:-1], 6)
    restored = postgres_database_factory()
    params = conninfo_to_dict(restored)
    password = params.pop("password", "")
    result = subprocess.run(
        [str(Path(config["bin_directory"]) / "pg_restore.exe"), "--exit-on-error", "--no-password",
         "--dbname", make_conninfo(**params), report["backup"]],
        env={**os.environ, "PGPASSWORD": password}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, "Vocabulary backup restore failed"
    with psycopg.connect(restored) as connection:
        assert connection.execute("SELECT to_regclass('topik_bank.vocabulary')").fetchone()[0] is None
        assert connection.execute("SELECT count(*) FROM topik_bank.schema_migrations").fetchone()[0] == 11


@pytest.mark.parametrize("failure", ["backup", "sql", "verification"])
def test_failure_rolls_back_schema_and_rows(local_bank, monkeypatch, failure):
    url, root, path, _ = local_bank

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret")

    if failure == "backup":
        monkeypatch.setattr(vocabulary, "_backup_database", fail)
    elif failure == "sql":
        original = vocabulary._upsert_vocabulary

        def fail_after_insert(connection, rows):
            original(connection, rows)
            connection.execute("SELECT 1 / 0")

        monkeypatch.setattr(vocabulary, "_upsert_vocabulary", fail_after_insert)
    else:
        monkeypatch.setattr(vocabulary, "_stored_rows", lambda connection: {})
    with pytest.raises(vocabulary.VocabularyImportError) as error:
        vocabulary.import_vocabulary(root, url, path)
    assert "synthetic-secret" not in str(error.value)
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT to_regclass('topik_bank.vocabulary')").fetchone()[0] is None
        assert connection.execute("SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s", (vocabulary.VERSION,)).fetchone() is None


def test_wrong_instance_rejected_before_backup(local_bank, monkeypatch):
    url, root, path, config = local_bank
    config["data_directory"] = str(root / "wrong-server")
    monkeypatch.setattr(vocabulary, "_backup_database", lambda *a, **kw: pytest.fail("must reject before backup"))
    with pytest.raises(vocabulary.VocabularyImportError):
        vocabulary.import_vocabulary(root, url, path)


def test_post_commit_verification_failure_is_reported(local_bank, monkeypatch):
    url, root, path, _ = local_bank
    original = vocabulary._stored_rows
    calls = 0

    def fail_final_check(connection):
        nonlocal calls
        calls += 1
        return {} if calls == 3 else original(connection)

    monkeypatch.setattr(vocabulary, "_stored_rows", fail_final_check)
    with pytest.raises(vocabulary.VocabularyImportError, match="커밋되었지만"):
        vocabulary.import_vocabulary(root, url, path)
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM topik_bank.vocabulary").fetchone()[0] == 3
