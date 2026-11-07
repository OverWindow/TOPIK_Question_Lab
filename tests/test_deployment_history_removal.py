import uuid

import psycopg
import pytest

from topik_question_lab.step_06_publication.bank_transfer import capture_source
from topik_question_lab.step_06_publication.postgres_inspection import inspect_database
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR, PostgresQuestionBank, PostgresUnavailableError
from topik_question_lab.step_06_publication.publication import build_set_draft
from test_bank_transfer import add_media
from test_unversioned_questions import candidate


VERSION = "013_remove_deployment_history"


@pytest.fixture
def bank_with_history(postgres_database_factory):
    url = postgres_database_factory()
    with psycopg.connect(url) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
        for path in sorted(MIGRATION_DIR.glob("*.sql")):
            if path.stem >= VERSION:
                break
            c.execute(path.read_text(encoding="utf-8"))
            c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (path.stem,))
    bank = PostgresQuestionBank(url)
    receipt = bank.publish_set(build_set_draft([candidate(n) for n in range(1, 51)], {}, 4, 0.0))
    add_media(url, receipt.set_id)
    run_id = uuid.uuid4()
    with psycopg.connect(url) as c:
        c.execute("""INSERT INTO topik_bank.deployment_runs(run_id,target_fingerprint,target_label,
            source_snapshot_hash,status,requested_set_count) VALUES (%s,%s,'former-target',%s,'succeeded',1)""",
            (run_id, "a" * 64, "b" * 64))
        c.execute("""INSERT INTO topik_bank.deployment_run_sets
            (run_id,set_id,set_sequence,source_snapshot_hash,status) VALUES (%s,%s,1,%s,'succeeded')""",
            (run_id, receipt.set_id, "c" * 64))
    return url, bank


def test_removal_preserves_questions_sets_media_and_prior_migrations(bank_with_history):
    url, bank = bank_with_history
    before = capture_source(url).rows
    with psycopg.connect(url) as c:
        versions = c.execute("SELECT * FROM topik_bank.schema_migrations ORDER BY version").fetchall()
    assert bank.ensure_schema() == [VERSION]
    assert inspect_database(url).ready
    assert capture_source(url).rows == before
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_regclass('topik_bank.deployment_runs'), to_regclass('topik_bank.deployment_run_sets')").fetchone() == (None, None)
        assert c.execute("SELECT * FROM topik_bank.schema_migrations WHERE version <> %s ORDER BY version", (VERSION,)).fetchall() == versions
    assert bank.ensure_schema() == []
    with psycopg.connect(url) as c:
        c.execute((MIGRATION_DIR / (VERSION + ".sql")).read_text(encoding="utf-8"))
    assert capture_source(url).rows == before


@pytest.mark.parametrize("dependency", [
    "CREATE VIEW public.synthetic_history AS SELECT run_id FROM topik_bank.deployment_runs",
    "CREATE TABLE public.synthetic_history(run_id UUID REFERENCES topik_bank.deployment_runs(run_id))",
])
def test_unexpected_dependency_rolls_back_both_drops(bank_with_history, dependency):
    url, bank = bank_with_history
    before = capture_source(url).rows
    with psycopg.connect(url) as c:
        c.execute(dependency)
        history = c.execute("SELECT to_jsonb(r) FROM topik_bank.deployment_runs r").fetchall()
        sets = c.execute("SELECT to_jsonb(s) FROM topik_bank.deployment_run_sets s").fetchall()
    with pytest.raises(PostgresUnavailableError):
        bank.ensure_schema()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_jsonb(r) FROM topik_bank.deployment_runs r").fetchall() == history
        assert c.execute("SELECT to_jsonb(s) FROM topik_bank.deployment_run_sets s").fetchall() == sets
        assert c.execute("SELECT to_regclass('public.synthetic_history') IS NOT NULL").fetchone()[0]
        assert not c.execute("SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s", (VERSION,)).fetchone()
    assert capture_source(url).rows == before
