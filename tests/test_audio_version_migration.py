import uuid

import psycopg
import pytest

from topik_question_lab.common.question_bank.postgres_storage import MIGRATION_DIR, PostgresQuestionBank, PostgresUnavailableError


VERSION = "016_exam_track_v4_only"


@pytest.fixture
def previous_bank(postgres_database_factory):
    url = postgres_database_factory()
    with psycopg.connect(url) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
        for path in sorted(MIGRATION_DIR.glob("*.sql")):
            if path.stem >= VERSION:
                break
            c.execute(path.read_text(encoding="utf-8"))
            c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (path.stem,))
    return url


def insert_audio(c, version=None):
    columns = "audio_asset_id,source_hash,model_name,female_voice,male_voice,storage_bucket,storage_path,storage_url,byte_size,script_snapshot"
    values = "%s,%s,'test','female','male','test','audio.mp3','https://example.invalid/audio.mp3',1,'{}'"
    params = [uuid.uuid4(), uuid.uuid4().hex * 2]
    if version is not None:
        columns += ",narration_version"
        values += ",%s"
        params.append(version)
    return c.execute(f"INSERT INTO topik_bank.tts_audio_assets({columns}) VALUES ({values}) RETURNING audio_asset_id,narration_version", params).fetchone()


@pytest.mark.parametrize("schema", ["migrations", "target"])
def test_new_banks_default_to_v4_and_reject_legacy_versions_and_missing_script(postgres_database_factory, schema):
    url = postgres_database_factory()
    if schema == "migrations":
        PostgresQuestionBank(url).ensure_schema()
    else:
        with psycopg.connect(url) as c:
            c.execute((MIGRATION_DIR.parent / "target_bank.sql").read_text(encoding="utf-8"))
    with psycopg.connect(url) as c:
        aid, version = insert_audio(c)
        assert version == "exam_track_v4"
        for legacy in ("dialogue_v1", "exam_track_v2", "exam_track_v3"):
            with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
                insert_audio(c, legacy)
        with pytest.raises(psycopg.errors.NotNullViolation), c.transaction():
            c.execute("UPDATE topik_bank.tts_audio_assets SET script_snapshot=NULL WHERE audio_asset_id=%s", (aid,))
    PostgresQuestionBank(url).ensure_schema()
    assert PostgresQuestionBank(url).ensure_schema() == []


@pytest.mark.parametrize("legacy", ["dialogue_v1", "exam_track_v2", "exam_track_v3"])
def test_upgrade_rejects_legacy_audio_without_deleting_or_relabeling(previous_bank, legacy):
    url = previous_bank
    with psycopg.connect(url) as c:
        aid, _ = insert_audio(c, legacy)
        before = c.execute("SELECT to_jsonb(a) FROM topik_bank.tts_audio_assets a").fetchall()
    with pytest.raises(PostgresUnavailableError, match="Legacy audio remains"):
        PostgresQuestionBank(url).ensure_schema()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_jsonb(a) FROM topik_bank.tts_audio_assets a").fetchall() == before
        assert c.execute("SELECT column_default FROM information_schema.columns WHERE table_schema='topik_bank' AND table_name='tts_audio_assets' AND column_name='narration_version'").fetchone() == ("'dialogue_v1'::text",)
        assert not c.execute("SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s", (VERSION,)).fetchone()


@pytest.mark.parametrize("named_snapshot_check", [False, True])
def test_upgrade_preserves_v4_audio_and_supports_existing_constraint_names(previous_bank, named_snapshot_check):
    url = previous_bank
    with psycopg.connect(url) as c:
        if named_snapshot_check:
            c.execute("ALTER TABLE topik_bank.tts_audio_assets RENAME CONSTRAINT tts_audio_assets_check TO tts_audio_assets_script_snapshot_check")
        insert_audio(c, "exam_track_v4")
        before = c.execute("SELECT to_jsonb(a) FROM topik_bank.tts_audio_assets a").fetchall()
    bank = PostgresQuestionBank(url)
    assert bank.ensure_schema() == [VERSION]
    assert bank.ensure_schema() == []
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_jsonb(a) FROM topik_bank.tts_audio_assets a").fetchall() == before
        assert insert_audio(c)[1] == "exam_track_v4"
        assert c.execute("SELECT is_nullable FROM information_schema.columns WHERE table_schema='topik_bank' AND table_name='tts_audio_assets' AND column_name='script_snapshot'").fetchone() == ("NO",)
