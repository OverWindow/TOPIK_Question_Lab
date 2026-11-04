from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
from threading import Event

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from topik_question_lab.maintenance import bank_migrations as migration
from topik_question_lab.step_06_publication import postgres_storage


def test_unconfigured_database_does_not_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(migration.psycopg, "connect", lambda *a, **k: pytest.fail("must not connect"))
    assert migration.migrate_local_bank(tmp_path, "") == ([], None)


def test_other_workspace_is_rejected_before_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(migration.psycopg, "connect", lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(migration.StartupMigrationError, match="이 저장소"):
        migration.migrate_local_bank(tmp_path, "postgresql://localhost/other")


def test_startup_caches_success_but_retries_failure(tmp_path, monkeypatch):
    from topik_question_lab.ui import startup
    calls = []

    def prepare(*args):
        calls.append(args)
        if len(calls) == 1:
            raise migration.StartupMigrationError("retry")
        return [], None

    monkeypatch.setattr(startup, "migrate_local_bank", prepare)
    with pytest.raises(migration.StartupMigrationError):
        startup.prepare_local_database(str(tmp_path), "test", ())
    assert startup.prepare_local_database(str(tmp_path), "test", ()) == ([], None)
    assert startup.prepare_local_database(str(tmp_path), "test", ()) == ([], None)
    assert len(calls) == 2
    startup.prepare_local_database(str(tmp_path), "test", (("010", 1),))
    assert len(calls) == 3


def test_main_stops_before_worker_recovery_when_preparation_fails(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from topik_question_lab.ui import startup, runner_service

    def fail(*args):
        raise migration.StartupMigrationError("DB preparation failed")

    monkeypatch.setattr(startup, "prepare_local_database", fail)
    monkeypatch.setattr(runner_service, "get_runner", lambda *a: pytest.fail("must not recover work"))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "topik_question_lab/main.py")).run()
    assert not app.exception
    assert [error.value for error in app.error] == ["DB preparation failed"]
    assert any(button.label == "DB 준비 다시 시도" for button in app.button)


def test_main_runs_without_database_configuration():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "topik_question_lab/main.py"),
                            default_timeout=20).run()
    assert not app.exception
    assert not app.error


@pytest.fixture
def old_bank(postgres_database_factory, tmp_path, monkeypatch):
    binaries = os.getenv("TEST_PG_BIN_DIR")
    if not binaries:
        pytest.skip("TEST_PG_BIN_DIR supplies local pg_dump/pg_restore")
    url = postgres_database_factory()
    with psycopg.connect(url) as c:
        data_directory = c.execute("SHOW data_directory").fetchone()[0]
        c.execute("CREATE SCHEMA topik_bank")
        c.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
        for path in sorted(migration.MIGRATION_DIR.glob("*.sql")):
            if path.stem >= migration.AUTO_VERSION:
                break
            c.execute(path.read_text(encoding="utf-8"))
            c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (path.stem,))
        c.execute("""INSERT INTO topik_bank.question_sets
            (set_id,section,generator_provider,generator_model,generator_version,set_sequence,default_target_level)
            VALUES ('00000000-0000-0000-0000-000000000001','reading','test','test','test',1,4)""")
        c.execute("""INSERT INTO topik_bank.items
            (item_id,source_key,section,type_slot,item_type,primary_skill,target_level,stem_length,choice_count,
             generator_provider,generator_model,generator_version,prompt_version,stem,choices,correct_answer,
             content_json,source_provenance,content_hash,set_id,position)
            VALUES ('00000000-0000-0000-0000-000000000002','synthetic','reading',1,'test','test',4,4,4,
             'test','test','test',repeat('a',64),'synthetic','["a","b","c","d"]',1,'{}','{}',repeat('b',64),
             '00000000-0000-0000-0000-000000000001',1)""")
    config = {"bin_directory": binaries, "data_directory": data_directory}
    monkeypatch.setattr(migration, "_local_config", lambda *args: config)
    return url, tmp_path, config


def old_contents(url):
    with psycopg.connect(url) as c:
        return [c.execute(f"SELECT to_jsonb(t) - 'exam_format' FROM topik_bank.{table} t ORDER BY 1").fetchall()
                for table in ("items", "question_sets")]


def assert_unmigrated(url):
    with psycopg.connect(url) as c:
        assert not c.execute("SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s", (migration.AUTO_VERSION,)).fetchall()
        assert not c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='topik_bank' AND column_name='exam_format'").fetchall()


def test_upgrade_preserves_contents_and_backup_restores(old_bank, postgres_database_factory):
    url, root, config = old_bank
    before = old_contents(url)
    versions, backup = migration.migrate_local_bank(root, url)
    assert versions == [migration.AUTO_VERSION]
    assert backup.is_file()
    assert old_contents(url) == before
    with psycopg.connect(url) as c:
        assert c.execute("SELECT exam_format FROM topik_bank.items").fetchall() == [("pbt",)]
        migration.require_media_schema(c)
    assert migration.migrate_local_bank(root, url) == ([], None)
    assert len(list((root / "backups/postgres").glob("*.dump"))) == 1
    restored = postgres_database_factory()
    params = conninfo_to_dict(restored)
    password = params.pop("password", "")
    result = subprocess.run([str(Path(config["bin_directory"]) / "pg_restore.exe"),
                             "--exit-on-error", "--no-password", "--dbname", make_conninfo(**params), str(backup)],
                            env={**os.environ, "PGPASSWORD": password},
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    assert result.returncode == 0, "Backup restore failed"
    assert old_contents(restored) == before
    assert_unmigrated(restored)


@pytest.mark.parametrize("failure", ["backup", "validation", "sql"])
def test_failed_upgrade_leaves_schema_and_records_unchanged(old_bank, monkeypatch, failure):
    url, root, _ = old_bank
    before = old_contents(url)

    def fail(*args):
        raise RuntimeError("synthetic-secret")

    if failure == "backup":
        monkeypatch.setattr(migration, "_backup_database", fail)
    elif failure == "validation":
        monkeypatch.setattr(migration, "require_media_schema", fail)
    else:
        original = postgres_storage.PostgresQuestionBank._apply_migrations

        def fail_sql(self, c):
            original(self, c)
            c.execute("SELECT 1 / 0")

        monkeypatch.setattr(postgres_storage.PostgresQuestionBank, "_apply_migrations", fail_sql)
    with pytest.raises(migration.StartupMigrationError) as error:
        migration.migrate_local_bank(root, url)
    assert "synthetic-secret" not in str(error.value)
    assert_unmigrated(url)
    assert old_contents(url) == before


def test_older_or_unreviewed_migrations_are_not_run(old_bank, monkeypatch):
    url, root, _ = old_bank
    monkeypatch.setattr(migration, "_backup_database", lambda *a: pytest.fail("must reject before backup"))
    with psycopg.connect(url) as c:
        c.execute("DELETE FROM topik_bank.schema_migrations WHERE version='009_pending_media_review'")
    with pytest.raises(migration.StartupMigrationError, match="수동 검토"):
        migration.migrate_local_bank(root, url)
    with psycopg.connect(url) as c:
        c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES ('009_pending_media_review')")
    directory = root / "migrations"
    directory.mkdir()
    for path in migration.MIGRATION_DIR.glob("*.sql"):
        (directory / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (directory / "011_unreviewed.sql").write_text("SELECT 1", encoding="utf-8")
    monkeypatch.setattr(migration, "MIGRATION_DIR", directory)
    with pytest.raises(migration.StartupMigrationError, match="수동 검토"):
        migration.migrate_local_bank(root, url)
    assert_unmigrated(url)


def test_concurrent_startups_apply_and_back_up_once(old_bank, monkeypatch):
    url, root, _ = old_bank
    original = migration._backup_database
    entered, release = Event(), Event()
    calls = []

    def backup(*args):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(migration, "_backup_database", backup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(migration.migrate_local_bank, root, url)
        assert entered.wait(10)
        second = pool.submit(migration.migrate_local_bank, root, url)
        release.set()
        results = [first.result(timeout=30), second.result(timeout=30)]
    assert sorted(len(result[0]) for result in results) == [0, 1]
    assert len(calls) == 1
