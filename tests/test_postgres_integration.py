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


def _draft(offset: int = 0):
    values = []
    for slot in range(1, 51):
        values.append(
            PublicationCandidate(
                section="reading",
                question_type="integration_test",
                generated_id=offset + slot,
                run_id=1,
                provider_key="codex_integration",
                model_id="codex-integration-model",
                backend="test",
                question={
                    "type_slot": slot,
                    "stem": f"통합 테스트 {offset + slot}번 ( ).",
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
    first_draft = _draft()
    second_draft = _draft(100)
    individual_only_draft = _draft(200)
    set_ids = []
    try:
        _cleanup_integration_data(psycopg)
        item_receipt = bank.publish_items(first_draft.items)
        assert item_receipt.total_items == 50
        first = bank.publish_set(first_draft)
        retry = bank.publish_set(first_draft)
        assert first.created_set_version
        assert first.set_sequence == 1
        assert not retry.created_set_version
        assert first.set_id == retry.set_id
        assert first.set_version == retry.set_version
        assert retry.set_sequence == 1

        second = bank.publish_set(second_draft)
        assert second.created_set_version
        assert second.set_sequence == 2
        assert second.set_id != first.set_id
        assert second.set_id == str(second_draft.set_id_for_sequence(2))
        set_ids = [first.set_id, second.set_id]

        current = [value for value in bank.list_current_items() if value["item_type"] == "integration_test"]
        contents = [value for value in bank.list_current_set_contents() if str(value["set_id"]) in set_ids]
        assert len(current) == 100
        assert len(contents) == 100
        assert {value["set_sequence"] for value in contents} == {1, 2}
        assert len(bank.list_all_set_source_keys() & {item.candidate.source_key for item in first_draft.items}) == 50

        bank.publish_items(individual_only_draft.items)
        individual_keys = {item.candidate.source_key for item in individual_only_draft.items}
        assert not bank.list_all_set_source_keys() & individual_keys

        overlap_candidates = [
            first_draft.items[0].candidate,
            *(item.candidate for item in individual_only_draft.items[1:]),
        ]
        overlap_metadata = {
            value.source_key: PublicationItemMetadata(4, 0.0, "통합 테스트")
            for value in overlap_candidates
        }
        overlap_draft = build_set_draft(overlap_candidates, overlap_metadata, 4, 0.0)
        with pytest.raises(ValueError, match="이미 PostgreSQL 세트에 사용된 문항"):
            bank.publish_set(overlap_draft)
        assert bank.next_set_sequence("reading", "test", "codex_integration", "codex-integration-model") == 3
    finally:
        _cleanup_integration_data(psycopg)


def _cleanup_integration_data(psycopg):
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        set_ids = [
            row[0]
            for row in connection.execute(
                """SELECT set_id FROM topik_bank.question_sets
                   WHERE section = 'reading' AND generator_provider = 'test'
                     AND generator_model = 'codex_integration'
                     AND generator_version = 'codex-integration-model'"""
            ).fetchall()
        ]
        for set_id in set_ids:
            connection.execute("DELETE FROM topik_bank.question_set_items WHERE set_id = %s", (set_id,))
            connection.execute("DELETE FROM topik_bank.question_set_versions WHERE set_id = %s", (set_id,))
            connection.execute("DELETE FROM topik_bank.question_sets WHERE set_id = %s", (set_id,))
        connection.execute(
            "DELETE FROM topik_bank.item_versions WHERE item_id IN (SELECT item_id FROM topik_bank.items WHERE source_key LIKE 'reading:integration_test:%')"
        )
        connection.execute("DELETE FROM topik_bank.items WHERE source_key LIKE 'reading:integration_test:%'")
