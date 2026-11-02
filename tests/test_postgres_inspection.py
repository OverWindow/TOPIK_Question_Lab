import uuid

import psycopg

from topik_question_lab.step_06_publication.postgres_inspection import database_fingerprint, inspect_database, _safe_error
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank


def test_inspection_without_connection_and_credential_redaction():
    assert not inspect_database("").configured
    first = "postgresql://user:synthetic-password@localhost:5432/first"
    assert database_fingerprint(first) == database_fingerprint(first.replace("synthetic-password", "other"))
    assert database_fingerprint(first) != database_fingerprint(first.replace("/first", "/second"))
    assert "synthetic-password" not in _safe_error(ValueError(first + " synthetic-password"), first)


def test_inspection_and_history_need_only_local_connection(postgres_database_factory):
    url = postgres_database_factory()
    bank = PostgresQuestionBank(url)
    bank.ensure_schema()
    run_id, set_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(url) as c:
        c.execute("""INSERT INTO topik_bank.deployment_runs(run_id,target_fingerprint,target_label,
            source_snapshot_hash,status,requested_set_count) VALUES (%s,%s,'former-target',%s,'succeeded',1)""",
            (run_id, 'a' * 64, 'b' * 64))
        c.execute("""INSERT INTO topik_bank.deployment_run_sets
            (run_id,set_id,set_sequence,source_snapshot_hash,status) VALUES (%s,%s,1,%s,'succeeded')""",
            (run_id, set_id, 'c' * 64))
        before = c.execute("SELECT to_jsonb(r) FROM topik_bank.deployment_runs r").fetchall()
    status = inspect_database(url)
    assert status.ready and status.item_count == 0 and status.set_count == 0
    history = bank.list_deployment_runs()
    assert history[0]['run_id'] == run_id and history[0]['audited_set_count'] == 1
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_jsonb(r) FROM topik_bank.deployment_runs r").fetchall() == before
