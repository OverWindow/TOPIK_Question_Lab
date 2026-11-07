import psycopg

from topik_question_lab.step_06_publication.postgres_inspection import database_fingerprint, inspect_database, _safe_error
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank


def test_inspection_without_connection_and_credential_redaction():
    assert not inspect_database("").configured
    first = "postgresql://user:synthetic-password@localhost:5432/first"
    assert database_fingerprint(first) == database_fingerprint(first.replace("synthetic-password", "other"))
    assert database_fingerprint(first) != database_fingerprint(first.replace("/first", "/second"))
    assert "synthetic-password" not in _safe_error(ValueError(first + " synthetic-password"), first)


def test_inspection_is_readonly_without_deployment_history(postgres_database_factory):
    url = postgres_database_factory()
    bank = PostgresQuestionBank(url)
    bank.ensure_schema()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_regclass('topik_bank.deployment_runs'), to_regclass('topik_bank.deployment_run_sets')").fetchone() == (None, None)
        before = c.execute("SELECT * FROM topik_bank.schema_migrations ORDER BY version").fetchall()
    status = inspect_database(url)
    assert status.ready and status.item_count == 0 and status.set_count == 0
    with psycopg.connect(url) as c:
        assert c.execute("SELECT * FROM topik_bank.schema_migrations ORDER BY version").fetchall() == before
        assert c.execute("SELECT to_regclass('topik_bank.deployment_runs'), to_regclass('topik_bank.deployment_run_sets')").fetchone() == (None, None)
