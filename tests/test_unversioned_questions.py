from dataclasses import replace
import uuid

import pytest

from topik_question_lab.step_06_publication.direct_publication import expected_item, require_target_schema, PublicationBlocked
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR, PostgresQuestionBank, PostgresUnavailableError
from topik_question_lab.step_06_publication.publication import PublicationCandidate, build_item_drafts, build_set_draft


def candidate(slot=1, text="문제", offset=0):
    return PublicationCandidate(
        section="reading", question_type="synthetic", generated_id=offset + slot,
        run_id=1, provider_key="test", model_id="synthetic-model", backend="test",
        question={"type_slot": slot, "stem": text, "choices": ["가", "나", "다", "라"],
                  "answer": 1, "explanation": "해설", "target_grammar": "독해"},
        review={"approved": True}, validation=[], prompt_system="system", prompt_user="user",
        created_at="2026-01-01", source_db="synthetic.db",
    )


def draft_item(value):
    return build_item_drafts([value], {}, 4, 0.0)[0]


def legacy_database(factory):
    import psycopg
    url = factory()
    with psycopg.connect(url) as connection:
        connection.execute("CREATE SCHEMA topik_bank")
        connection.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
        for path in sorted(MIGRATION_DIR.glob("*.sql")):
            if path.stem.startswith("006"):
                break
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (path.stem,))
    return url


def insert_legacy_item(connection, value, version):
    from psycopg.types.json import Jsonb
    item = draft_item(value)
    connection.execute("INSERT INTO topik_bank.items(item_id,source_key) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                       (value.item_id, value.source_key))
    payload = {"item_id": value.item_id, "item_version": version, **expected_item(item), "predicted_difficulty": 0.0, "source_provenance": {}}
    payload.pop("exam_format")  # The legacy schema predates exam formats.
    names = list(payload)
    connection.execute(
        f"INSERT INTO topik_bank.item_versions({','.join(names)}) VALUES ({','.join(['%s'] * len(names))})",
        tuple(Jsonb(payload[k]) if k in {"choices", "content_json", "source_provenance"} else payload[k] for k in names),
    )


def insert_legacy_set(connection, item_id, sequence=1):
    set_id = uuid.uuid4()
    connection.execute("""INSERT INTO topik_bank.question_sets(
        set_id,section,generator_provider,generator_model,generator_version,set_sequence,
        default_target_level,default_predicted_difficulty,set_fingerprint)
        VALUES (%s,'reading','test','test','synthetic-model',%s,4,0.0,%s)""",
        (set_id, sequence, "a" * 64))
    connection.execute("INSERT INTO topik_bank.question_set_items(set_id,position,item_id,item_version) VALUES (%s,1,%s,1)",
                       (set_id, item_id))
    return set_id


def test_migration_preserves_published_contents_and_latest_unassigned_item(postgres_database_factory):
    import psycopg
    url = legacy_database(postgres_database_factory)
    assigned = candidate(text="세트에 발행한 내용")
    free = candidate(slot=2, text="미배정 이전 내용")
    latest_free = replace(free, question={**free.question, "stem": "미배정 최신 내용"})
    with psycopg.connect(url) as connection:
        for value in (assigned, free):
            insert_legacy_item(connection, value, 1)
        insert_legacy_item(connection, replace(assigned, question={**assigned.question, "stem": "미발행 수정"}), 2)
        insert_legacy_item(connection, latest_free, 2)
        set_id = insert_legacy_set(connection, assigned.item_id)
        connection.execute("""INSERT INTO topik_bank.deployment_runs(run_id,target_fingerprint,target_label,
            source_snapshot_hash,status,requested_set_count) VALUES (%s,%s,'test',%s,'succeeded',1)""",
            (uuid.uuid4(), "b" * 64, "c" * 64))
    bank = PostgresQuestionBank(url)
    assert bank.ensure_schema() == [path.stem for path in sorted(MIGRATION_DIR.glob("*.sql")) if path.stem >= "006"]
    assert bank.ensure_schema() == []
    with psycopg.connect(url) as connection:
        rows = connection.execute("SELECT source_key,stem,set_id,position FROM topik_bank.items ORDER BY type_slot").fetchall()
        assert rows == [(assigned.source_key, assigned.question["stem"], set_id, 1),
                        (free.source_key, latest_free.question["stem"], None, None)]
        assert connection.execute("SELECT to_regclass('topik_bank.deployment_runs'), to_regclass('topik_bank.deployment_run_sets')").fetchone() == (None, None)
        assert connection.execute("SELECT to_regclass('topik_bank.item_versions'),to_regclass('topik_bank.question_set_items')").fetchone() == (None, None)
        assert connection.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='topik_bank' AND column_name='item_version'").fetchone()[0] == 0
        require_target_schema(connection)
    assert bank.list_current_set_contents()[0]["stem"] == assigned.question["stem"]


def test_migration_refuses_multiple_set_memberships_atomically(postgres_database_factory):
    import psycopg
    url = legacy_database(postgres_database_factory)
    value = candidate()
    with psycopg.connect(url) as connection:
        insert_legacy_item(connection, value, 1)
        insert_legacy_set(connection, value.item_id, 1)
        insert_legacy_set(connection, value.item_id, 2)
    with pytest.raises(PostgresUnavailableError, match="multiple sets"):
        PostgresQuestionBank(url).ensure_schema()
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM topik_bank.question_set_items").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='topik_bank' AND table_name='items' AND column_name='set_id'").fetchone()[0] == 0


def test_migration_refuses_external_version_references(postgres_database_factory):
    import psycopg
    url = legacy_database(postgres_database_factory)
    with psycopg.connect(url) as connection:
        connection.execute("CREATE SCHEMA topik_app")
        connection.execute("""CREATE TABLE topik_app.answers(item_id UUID,item_version INTEGER,
            FOREIGN KEY (item_id,item_version) REFERENCES topik_bank.item_versions(item_id,item_version))""")
    with pytest.raises(PostgresUnavailableError, match="External tables"):
        PostgresQuestionBank(url).ensure_schema()
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT to_regclass('topik_bank.item_versions') IS NOT NULL").fetchone()[0]


def test_explicit_sync_updates_one_row_and_existing_set(postgres_database_factory):
    import psycopg
    from topik_question_lab.step_06_publication.postgres_inspection import inspect_database
    url = postgres_database_factory()
    bank = PostgresQuestionBank(url)
    bank.ensure_schema()
    values = [candidate(slot) for slot in range(1, 51)]
    first = bank.publish_set(build_set_draft(values, {}, 4, 0.0))
    assert first.created_items == 50
    retry = bank.publish_set(build_set_draft(values, {}, 4, 0.0))
    assert not retry.created_set and retry.reused_items == 50
    edited = replace(values[0], question={**values[0].question, "stem": "수정한 현재 내용"})
    updated = bank.publish_items([draft_item(edited)])
    assert updated.updated_items == 1 and updated.created_items == 0
    assert bank.publish_items([draft_item(edited)]).reused_items == 1
    wrong_slot = replace(edited, question={**edited.question, "type_slot": 2})
    with pytest.raises(PostgresUnavailableError, match="번호"):
        bank.publish_items([draft_item(wrong_slot)])
    updated_set = bank.publish_set(build_set_draft([edited, *values[1:]], {}, 5, 0.5))
    assert updated_set.set_id == first.set_id and updated_set.updated_items == 50
    assert inspect_database(url).ready
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 50
        assert connection.execute("SELECT item_id FROM topik_bank.items WHERE source_key=%s", (edited.source_key,)).fetchone()[0] == edited.item_id
        assert connection.execute("SELECT stem FROM topik_bank.current_set_contents WHERE set_id=%s AND position=1", (first.set_id,)).fetchone()[0] == edited.question["stem"]
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute("UPDATE topik_bank.items SET position=NULL WHERE item_id=%s", (edited.item_id,))


def test_local_schema_check_never_migrates_legacy_database(postgres_database_factory):
    import psycopg
    url = legacy_database(postgres_database_factory)
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as connection:
        with pytest.raises(PublicationBlocked, match="006"):
            require_target_schema(connection)
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT to_regclass('topik_bank.item_versions') IS NOT NULL").fetchone()[0]

