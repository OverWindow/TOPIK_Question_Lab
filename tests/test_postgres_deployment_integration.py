from __future__ import annotations

import os
import uuid

import pytest

from topik_question_lab.postgres_deployment import (
    SYNCED,
    DeploymentError,
    PostgresDeploymentService,
    same_database_configuration,
)
from topik_question_lab.postgres_storage import PostgresQuestionBank
from topik_question_lab.publication import (
    PublicationCandidate,
    PublicationItemMetadata,
    build_set_draft,
)


SOURCE_URL = os.getenv("TEST_SOURCE_DATABASE_URL", "").strip()
TARGET_URL = os.getenv("TEST_TARGET_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not SOURCE_URL or not TARGET_URL,
    reason="TEST_SOURCE_DATABASE_URL과 TEST_TARGET_DATABASE_URL이 설정되지 않았습니다.",
)


def _draft(offset: int):
    candidates = [
        PublicationCandidate(
            section="reading",
            question_type="deployment_integration",
            generated_id=offset + slot,
            run_id=1,
            provider_key="codex_deployment",
            model_id="codex-deployment-model",
            backend="test",
            question={
                "type_slot": slot,
                "stem": f"운영 배포 통합 테스트 {offset + slot}번 ( ).",
                "choices": ["가", "나", "다", "라"],
                "answer": 1,
                "explanation": "해설",
                "target_grammar": "배포 테스트",
            },
            review={"approved": True},
            validation=[],
            prompt_system="system",
            prompt_user=f"prompt {slot}",
            created_at="2026-01-01 00:00:00",
            source_db="deployment-integration.db",
        )
        for slot in range(1, 51)
    ]
    metadata = {
        value.source_key: PublicationItemMetadata(4, 0.0, "배포 테스트")
        for value in candidates
    }
    return build_set_draft(candidates, metadata, 4, 0.0)


def test_multi_set_deployment_rolls_back_as_one_transaction_then_retries():
    if same_database_configuration(SOURCE_URL, TARGET_URL):
        pytest.skip("원본과 대상 테스트 DB는 서로 달라야 합니다.")
    psycopg = pytest.importorskip("psycopg")
    source_bank = PostgresQuestionBank(SOURCE_URL)
    target_bank = PostgresQuestionBank(TARGET_URL)
    source_bank.ensure_schema()
    target_bank.ensure_schema()
    first_draft = _draft(7000)
    second_draft = _draft(8000)
    source_set_ids: list[str] = []
    dummy_set_id = str(uuid.uuid4())
    try:
        _cleanup(psycopg, SOURCE_URL)
        _cleanup(psycopg, TARGET_URL)
        first = source_bank.publish_set(first_draft)
        second = source_bank.publish_set(second_draft)
        source_set_ids = [first.set_id, second.set_id]

        with psycopg.connect(TARGET_URL) as connection:
            connection.execute(
                """INSERT INTO topik_bank.question_sets(
                       set_id, section, generator_provider, generator_model,
                       generator_version, set_sequence, review_status,
                       default_target_level, default_predicted_difficulty,
                       set_fingerprint
                   ) VALUES (%s, 'reading', 'test', 'codex_deployment',
                             'codex-deployment-model', 2, 'reviewed', 4, 0.0, %s)""",
                (dummy_set_id, "d" * 64),
            )

        service = PostgresDeploymentService(SOURCE_URL, TARGET_URL)
        with pytest.raises(DeploymentError, match="전체 롤백 완료"):
            service.deploy(source_set_ids)
        with psycopg.connect(TARGET_URL) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM topik_bank.question_sets WHERE set_id = %s",
                (first.set_id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM topik_bank.items WHERE source_key LIKE 'reading:deployment_integration:%'"
            ).fetchone()[0] == 0
            connection.execute("DELETE FROM topik_bank.question_sets WHERE set_id = %s", (dummy_set_id,))

        receipt = service.deploy(source_set_ids)
        assert receipt.status == "succeeded"
        assert receipt.transferred_set_count == 2
        statuses = [value for value in service.compare() if value.set_id in source_set_ids]
        assert len(statuses) == 2
        assert all(value.status == SYNCED for value in statuses)

        retry = service.deploy(source_set_ids)
        assert retry.status == "succeeded"
        assert retry.transferred_set_count == 0
        assert retry.reused_set_count == 2
        assert retry.created_row_count == 0
    finally:
        _cleanup(psycopg, TARGET_URL, source_set_ids, dummy_set_id)
        _cleanup(psycopg, SOURCE_URL, source_set_ids)


def _cleanup(psycopg, url: str, set_ids: list[str] | None = None, dummy_set_id: str = ""):
    with psycopg.connect(url) as connection:
        known_set_ids = list(set_ids or [])
        known_set_ids.extend(
            str(row[0])
            for row in connection.execute(
                """SELECT set_id FROM topik_bank.question_sets
                   WHERE section = 'reading' AND generator_provider = 'test'
                     AND generator_model = 'codex_deployment'
                     AND generator_version = 'codex-deployment-model'"""
            ).fetchall()
        )
        if dummy_set_id:
            known_set_ids.append(dummy_set_id)
        known_set_ids = list(dict.fromkeys(known_set_ids))
        if known_set_ids:
            known_set_uuids = [uuid.UUID(value) for value in known_set_ids]
            run_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT run_id FROM topik_bank.deployment_run_sets WHERE set_id = ANY(%s)",
                    (known_set_uuids,),
                ).fetchall()
            ]
            if run_ids:
                connection.execute(
                    "DELETE FROM topik_bank.deployment_runs WHERE run_id = ANY(%s)",
                    (run_ids,),
                )
            connection.execute(
                "DELETE FROM topik_bank.question_set_items WHERE set_id = ANY(%s)",
                (known_set_uuids,),
            )
            connection.execute(
                "DELETE FROM topik_bank.question_sets WHERE set_id = ANY(%s)",
                (known_set_uuids,),
            )
        connection.execute(
            """DELETE FROM topik_bank.item_versions
               WHERE item_id IN (
                   SELECT item_id FROM topik_bank.items
                   WHERE source_key LIKE 'reading:deployment_integration:%'
               )"""
        )
        connection.execute(
            "DELETE FROM topik_bank.items WHERE source_key LIKE 'reading:deployment_integration:%'"
        )
