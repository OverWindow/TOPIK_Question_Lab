from pathlib import Path
import os
import subprocess
import uuid

import psycopg
import pytest

from topik_question_lab.maintenance.item_audio_migration import migration_baseline, verify_migration, migrate_item_audio
from topik_question_lab.step_06_publication.audio_bindings import bind_audio
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR, PostgresQuestionBank
from topik_question_lab.step_06_publication.publication import build_set_draft
from test_unversioned_questions import candidate

MIGRATION = MIGRATION_DIR / "011_item_audio_only.sql"


@pytest.fixture
def legacy_audio(postgres_database_factory):
    url = postgres_database_factory()
    with psycopg.connect(url) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ DEFAULT now())")
        for path in sorted(MIGRATION_DIR.glob("*.sql")):
            if path.name >= MIGRATION.name:
                break
            c.execute(path.read_text(encoding="utf-8"))
            c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (path.stem,))
    bank = PostgresQuestionBank(url)
    sid = bank.publish_set(build_set_draft([candidate(n) for n in range(1, 51)], {}, 4, 0.0)).set_id
    with psycopg.connect(url) as c:
        items = {r[0]: (r[1], r[2]) for r in c.execute("SELECT position,item_id,content_hash FROM topik_bank.items")}
        assets = [uuid.uuid4() for _ in range(3)]
        for n, aid in enumerate(assets):
            c.execute("""INSERT INTO topik_bank.tts_audio_assets(audio_asset_id,source_hash,model_name,
                female_voice,male_voice,storage_bucket,storage_path,storage_url,byte_size,deleted_at,
                narration_version,script_snapshot)
                VALUES (%s,%s,'test','f','m','test','audio.mp3','https://example.invalid/audio.mp3',1,
                        CASE WHEN %s THEN now() END,'exam_track_v4','{}')""", (aid, str(n)*64, n == 2))
        for slot in (1, 2, 3, 5):
            c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
                VALUES (%s,%s,%s,%s)""", (*items[slot], assets[0], "a"*64))
        for content_hash, current in (("d"*64, True), ("e"*64, False)):
            c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash,is_current)
                VALUES (%s,%s,%s,%s,%s)""", (items[1][0], content_hash, assets[0], "a"*64, current))
        for slot, asset, current in ((1, 1, True), (1, 0, False), (2, 0, True), (6, 2, True)):
            c.execute("""INSERT INTO topik_bank.question_set_item_audio_bindings(set_id,position,audio_asset_id,source_hash,is_current)
                VALUES (%s,%s,%s,%s,%s)""", (sid, slot, assets[asset], "b"*64, current))
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE item_id=%s", (items[5][0],))
    return url, items, assets


def test_migration_preserves_all_history_and_current_priority(legacy_audio):
    url, items, assets = legacy_audio
    with psycopg.connect(url) as c:
        baseline = migration_baseline(c)
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        report = verify_migration(c, baseline)
        assert report["bindings"] == 10 and report["current_bindings"] == 4
        current = dict(c.execute("SELECT item_id,audio_asset_id FROM topik_bank.item_audio_bindings WHERE is_current"))
        assert current == {items[1][0]: assets[1], **{items[n][0]: assets[0] for n in (2, 3, 5)}}
        assert c.execute("SELECT count(*) FROM topik_bank.item_audio_bindings WHERE content_hash IS NULL").fetchone()[0] == 2
    # Schema preparation must never recreate the removed table on retries.
    PostgresQuestionBank(url).ensure_schema()
    assert PostgresQuestionBank(url).ensure_schema() == []
    with psycopg.connect(url) as c:
        verify_migration(c, baseline)


def test_dependent_object_rolls_back_entire_migration(legacy_audio):
    url, _, _ = legacy_audio
    with psycopg.connect(url) as c:
        baseline = migration_baseline(c)
        c.execute("CREATE VIEW public.synthetic_binding_reference AS SELECT * FROM topik_bank.question_set_item_audio_bindings")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        with psycopg.connect(url) as c:
            c.execute(MIGRATION.read_text(encoding="utf-8"))
    with psycopg.connect(url) as c:
        assert migration_baseline(c) == baseline
        assert not c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='topik_bank' AND column_name='binding_id'").fetchall()


def test_standalone_rebinding_is_idempotent_and_keeps_history(legacy_audio):
    url, items, assets = legacy_audio
    with psycopg.connect(url) as c:
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        iid, content_hash = items[5]
        old = c.execute("SELECT binding_id FROM topik_bank.item_audio_bindings WHERE item_id=%s AND is_current", (iid,)).fetchone()[0]
        assert bind_audio(c, iid, content_hash, assets[0], "a"*64) == old
        new = bind_audio(c, iid, content_hash, assets[1], "new".ljust(64, "0"))
        assert new != old
        assert bind_audio(c, iid, content_hash, assets[1], "new".ljust(64, "0")) == new
        assert c.execute("SELECT count(*),count(*) FILTER(WHERE is_current) FROM topik_bank.item_audio_bindings WHERE item_id=%s", (iid,)).fetchone() == (2, 1)
        with pytest.raises(psycopg.errors.UniqueViolation), c.transaction():
            c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
                VALUES (%s,%s,%s,%s)""", (iid, "f"*64, assets[0], "a"*64))
        with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
            c.execute("UPDATE topik_bank.item_audio_bindings SET content_hash=NULL WHERE binding_id=%s", (new,))


def test_failed_backup_does_not_change_database(legacy_audio, tmp_path, monkeypatch):
    from topik_question_lab.maintenance import item_audio_migration as migration
    url, _, _ = legacy_audio
    with psycopg.connect(url) as c:
        baseline = migration_baseline(c)
    def fail(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(migration, "_backup_database", fail)
    with pytest.raises(migration.StartupMigrationError):
        migrate_item_audio(tmp_path, url, {})
    with psycopg.connect(url) as c:
        assert migration_baseline(c) == baseline


def test_schema_preparation_cannot_bypass_backup(legacy_audio):
    from topik_question_lab.step_06_publication.postgres_storage import PostgresUnavailableError
    url, _, _ = legacy_audio
    with psycopg.connect(url) as c:
        baseline = migration_baseline(c)
    with pytest.raises(PostgresUnavailableError, match="백업"):
        PostgresQuestionBank(url).ensure_schema()
    with psycopg.connect(url) as c:
        assert migration_baseline(c) == baseline


def test_unified_target_schema_can_be_prepared_without_recreating_legacy_table(postgres_database_factory):
    url = postgres_database_factory()
    with psycopg.connect(url) as c:
        c.execute((MIGRATION_DIR.parent / "target_bank.sql").read_text(encoding="utf-8"))
    PostgresQuestionBank(url).ensure_schema()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_regclass('topik_bank.question_set_item_audio_bindings')").fetchone() == (None,)


def test_startup_never_applies_table_removal(legacy_audio, tmp_path, monkeypatch):
    from topik_question_lab.maintenance import bank_migrations as startup
    url, _, _ = legacy_audio
    with psycopg.connect(url) as c:
        directory = c.execute("SHOW data_directory").fetchone()[0]
        before = migration_baseline(c)
    monkeypatch.setattr(startup, "_local_config", lambda *args: {"data_directory": directory})
    monkeypatch.setattr(startup, "_backup_database", lambda *args: pytest.fail("startup must not migrate 011"))
    with pytest.raises(startup.StartupMigrationError, match="011"):
        startup.migrate_local_bank(tmp_path, url)
    with psycopg.connect(url) as c:
        assert migration_baseline(c) == before


def test_manual_migration_backup_restores_original_history(legacy_audio, tmp_path, postgres_database_factory):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    binaries = os.getenv("TEST_PG_BIN_DIR")
    if not binaries:
        pytest.skip("TEST_PG_BIN_DIR supplies local backup tools")
    url, _, _ = legacy_audio
    with psycopg.connect(url, options="-c timezone=UTC") as c:
        baseline = migration_baseline(c)
    report = migrate_item_audio(tmp_path, url, {"bin_directory": binaries})
    assert report["bindings"] == 10 and Path(report["backup"]).is_file()
    assert migrate_item_audio(tmp_path, url, {"bin_directory": binaries}) == {"already_migrated": True}
    restored = postgres_database_factory()
    params = conninfo_to_dict(restored)
    password = params.pop("password", "")
    result = subprocess.run([str(Path(binaries) / "pg_restore.exe"), "--exit-on-error", "--no-password",
        "--dbname", make_conninfo(**params), report["backup"]], env={**os.environ, "PGPASSWORD": password},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, "Backup restore failed"
    with psycopg.connect(restored, options="-c timezone=UTC") as c:
        assert migration_baseline(c) == baseline
