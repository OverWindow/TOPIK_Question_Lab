import os

import pytest

from topik_question_lab.postgres_storage import PostgresQuestionBank
from topik_question_lab.publication import (
    PublicationCandidate,
    PublicationItemMetadata,
    build_set_draft,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL이 설정되지 않았습니다.")


def _draft():
    values = []
    for slot in range(1, 51):
        values.append(
            PublicationCandidate(
                section="reading",
                question_type="integration_test",
                generated_id=slot,
                run_id=1,
                provider_key="codex_integration",
                model_id="codex-integration-model",
                backend="test",
                question={
                    "type_slot": slot,
                    "stem": f"통합 테스트 {slot}번 ( ).",
                    "choices": ["가", "나", "다", "라"],
                    "answer": 1,
                    "explanation": "해설",
                    "target_grammar": "통합 테스트",
                },
                review={"approved": True},
                validation=[],
                prompt_system="system",
                prompt_user=f"prompt {slot}",
                created_at="2026-01-01 00:00:00",
                source_db="integration-test.db",
            )
        )
    metadata = {
        value.source_key: PublicationItemMetadata(4, 0.0, "통합 테스트") for value in values
    }
    return build_set_draft(values, metadata, 4, 0.0)


def test_postgres_migration_publish_and_idempotency():
    psycopg = pytest.importorskip("psycopg")
    bank = PostgresQuestionBank(TEST_DATABASE_URL)
    bank.ensure_schema()
    draft = _draft()
    try:
        item_receipt = bank.publish_items(draft.items)
        assert item_receipt.total_items == 50
        first = bank.publish_set(draft)
        second = bank.publish_set(draft)
        assert first.created_set_version
        assert not second.created_set_version
        assert first.set_id == second.set_id
        assert first.set_version == second.set_version
        current = [value for value in bank.list_current_items() if value["item_type"] == "integration_test"]
        contents = [value for value in bank.list_current_set_contents() if value["set_id"] == draft.set_id]
        assert len(current) == 50
        assert len(contents) == 50
        assert [value["position"] for value in contents] == list(range(1, 51))
    finally:
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            connection.execute("DELETE FROM topik_bank.question_set_items WHERE set_id = %s", (draft.set_id,))
            connection.execute("DELETE FROM topik_bank.question_set_versions WHERE set_id = %s", (draft.set_id,))
            connection.execute("DELETE FROM topik_bank.question_sets WHERE set_id = %s", (draft.set_id,))
            connection.execute(
                "DELETE FROM topik_bank.item_versions WHERE item_id IN (SELECT item_id FROM topik_bank.items WHERE source_key LIKE 'reading:integration_test:%')"
            )
            connection.execute("DELETE FROM topik_bank.items WHERE source_key LIKE 'reading:integration_test:%'")
