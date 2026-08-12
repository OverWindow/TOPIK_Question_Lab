from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from topik_question_lab.postgres_deployment import (
    CONFLICT,
    MISSING,
    SYNCED,
    TARGET_ONLY,
    _DatabaseSnapshot,
    _build_plan,
    _safe_error,
    compare_snapshots,
    database_label,
    same_database_configuration,
)


SET_ID = "10000000-0000-0000-0000-000000000001"
ITEM_ID = "20000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(*, include_set: bool = True, stem: str = "문제", created_at=NOW):
    set_row = {
        "set_id": UUID(SET_ID),
        "section": "reading",
        "generator_provider": "api",
        "generator_model": "deepseek",
        "generator_version": "deepseek_v4_pro",
        "set_sequence": 2,
        "created_at": created_at,
    }
    set_version = {
        "set_id": UUID(SET_ID),
        "set_version": 1,
        "review_status": "reviewed",
        "default_target_level": 4,
        "default_predicted_difficulty": 0.0,
        "set_fingerprint": "a" * 64,
        "published_at": created_at,
    }
    membership = {
        "set_id": UUID(SET_ID),
        "set_version": 1,
        "position": 1,
        "item_id": UUID(ITEM_ID),
        "item_version": 1,
    }
    item = {"item_id": UUID(ITEM_ID), "source_key": "reading:test:1", "created_at": created_at}
    item_version = {
        "item_id": UUID(ITEM_ID),
        "item_version": 1,
        "section": "reading",
        "type_slot": 1,
        "item_type": "test",
        "primary_skill": "독해",
        "target_level": 4,
        "predicted_difficulty": 0.0,
        "irt_difficulty": None,
        "irt_discrimination": None,
        "stem_length": len(stem),
        "choice_count": 4,
        "generator_provider": "api",
        "generator_model": "deepseek",
        "generator_version": "deepseek_v4_pro",
        "prompt_version": "b" * 64,
        "review_status": "reviewed",
        "stem": stem,
        "choices": ["1", "2", "3", "4"],
        "correct_answer": 1,
        "explanation": "해설",
        "content_json": {"stem": stem},
        "source_provenance": {"source_key": "reading:test:1"},
        "content_hash": "c" * 64,
        "created_at": created_at,
    }
    sets = {SET_ID: set_row} if include_set else {}
    set_versions = {(SET_ID, 1): set_version} if include_set else {}
    memberships = {(SET_ID, 1, 1): membership} if include_set else {}
    items = {ITEM_ID: item} if include_set else {}
    item_versions = {(ITEM_ID, 1): item_version} if include_set else {}
    return _DatabaseSnapshot(
        sets=sets,
        set_id_by_identity={
            ("reading", "api", "deepseek", "deepseek_v4_pro", 2): SET_ID
        } if include_set else {},
        set_versions=set_versions,
        set_version_by_fingerprint={(SET_ID, "a" * 64): 1} if include_set else {},
        memberships=memberships,
        membership_position_by_item={(SET_ID, 1, ITEM_ID): 1} if include_set else {},
        items=items,
        item_id_by_source={"reading:test:1": ITEM_ID} if include_set else {},
        item_versions=item_versions,
        item_version_by_hash={(ITEM_ID, "c" * 64): 1} if include_set else {},
    )


def test_missing_set_is_deployable_and_preview_counts_unique_rows():
    source = _snapshot()
    target = _snapshot(include_set=False)
    status = compare_snapshots(source, target)[0]
    plan = _build_plan(source, target, (SET_ID,))

    assert status.status == MISSING
    assert status.deployable
    assert status.expected_memberships == 1
    assert plan.created_row_count == 5
    assert plan.reused_row_count == 0


def test_semantically_equal_rows_ignore_creation_and_publication_times():
    source = _snapshot(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    target = _snapshot(created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))

    status = compare_snapshots(source, target)[0]

    assert status.status == SYNCED
    assert status.exact_memberships == 1
    assert status.missing_rows == 0
    assert status.conflict_rows == 0


def test_same_item_version_with_different_content_is_a_conflict():
    source = _snapshot(stem="원본 문제")
    target = _snapshot(stem="운영에서 변경된 문제")

    status = compare_snapshots(source, target)[0]

    assert status.status == CONFLICT
    assert not status.deployable
    assert any("item_version" in reason for reason in status.reasons)


def test_identity_collision_with_another_set_id_is_a_conflict():
    source = _snapshot()
    target = _snapshot(include_set=False)
    other_id = "10000000-0000-0000-0000-000000000099"
    target.set_id_by_identity[("reading", "api", "deepseek", "deepseek_v4_pro", 2)] = other_id

    status = compare_snapshots(source, target)[0]

    assert status.status == CONFLICT
    assert any("다른 set_id" in reason for reason in status.reasons)


def test_target_only_sets_are_reported_without_becoming_deployable():
    source = _snapshot(include_set=False)
    target = _snapshot()

    status = compare_snapshots(source, target)[0]

    assert status.status == TARGET_ONLY
    assert not status.deployable


def test_connection_identity_ignores_password_but_not_database_name():
    first = "postgresql://user:first-secret@db.example.com:5432/topik"
    same = "postgresql://user:second-secret@db.example.com:5432/topik"
    other = "postgresql://user:first-secret@db.example.com:5432/production"

    assert same_database_configuration(first, same)
    assert not same_database_configuration(first, other)
    assert "secret" not in database_label(first)


def test_connection_errors_mask_source_and_target_passwords():
    source = "postgresql://user:source-secret@source.example.com/topik"
    target = "postgresql://user:target-secret@target.example.com/postgres"
    error = RuntimeError(f"failed {source} source-secret target-secret")

    message = _safe_error(error, source, target)

    assert "source-secret" not in message
    assert "target-secret" not in message
    assert "[DATABASE_URL]" in message
