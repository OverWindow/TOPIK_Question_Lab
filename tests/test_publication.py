from pathlib import Path

import pytest

from topik_question_lab.models import ProviderResult, Review
from topik_question_lab.postgres_storage import (
    _insert_set_items,
    _resolve_set_membership,
    _safe_error,
    _source_provenance,
)
from topik_question_lab.publication import (
    CandidateCatalog,
    LocalPublicationRecord,
    PublicationCandidate,
    PublicationItemMetadata,
    build_item_drafts,
    build_set_draft,
    collect_candidate_catalog,
    display_choices,
    display_stem,
    group_by_slot,
    reconcile_catalog,
    unused_set_candidates,
    validate_complete_selection,
)
from topik_question_lab.storage import Storage


def candidate(slot: int, *, section: str = "reading", source_id: int | None = None) -> PublicationCandidate:
    question = {
        "type_slot": slot,
        "question_type": "grammar_blank",
        "stem": f"{slot}번 문항 ( ).",
        "choices": ["가", "나", "다", "라"],
        "answer": 1,
        "explanation": "해설",
        "target_grammar": "문법",
    }
    if section == "listening":
        question = {
            "type_slot": slot,
            "question_type": "next_response",
            "dialogue_turns": [{"speaker": "여자", "text": f"{slot}번 대화입니다."}],
            "question_prompt": "이어질 말을 고르십시오.",
            "choices": ["가", "나", "다", "라"],
            "answer": 1,
            "explanation": "해설",
            "target_skill": "이어질 말",
        }
    return PublicationCandidate(
        section=section,
        question_type=str(question["question_type"]),
        generated_id=source_id or slot,
        run_id=1,
        provider_key="gpt_5_6_luna",
        model_id="gpt-5.6-luna",
        backend="chatkhu",
        question=question,
        review={"approved": True},
        validation=[],
        prompt_system="system",
        prompt_user=f"prompt {slot}",
        created_at="2026-01-01 00:00:00",
        source_db="data/types/sample.db",
    )


def test_complete_set_requires_one_item_for_every_slot():
    values = [candidate(slot) for slot in range(1, 51)]
    assert len(validate_complete_selection(values)) == 50

    with pytest.raises(ValueError, match="누락"):
        validate_complete_selection(values[:-1])

    with pytest.raises(ValueError, match="중복"):
        validate_complete_selection([*values, candidate(1, source_id=99)])


def test_set_draft_derives_metadata_length_and_stable_hashes():
    values = [candidate(slot) for slot in range(1, 51)]
    metadata = {
        value.source_key: PublicationItemMetadata(4, 0.2, "조건 표현") for value in values
    }
    first = build_set_draft(values, metadata, 4, 0.0)
    second = build_set_draft(values, metadata, 4, 0.0)

    assert first.set_id == second.set_id
    assert first.items[0].content_hash == second.items[0].content_hash
    assert first.items[0].stem_length == len("1번 문항 ( ).")
    assert first.items[0].choice_count == 4
    assert first.items[0].metadata.primary_skill == "조건 표현"
    assert first.set_id_for_sequence(1) == first.set_id
    assert first.set_id_for_sequence(2) != first.set_id
    assert first.set_id_for_sequence(2) == second.set_id_for_sequence(2)


def test_used_source_keys_are_excluded_even_when_candidate_content_changes():
    original = candidate(1)
    revised = PublicationCandidate(
        **{
            **original.__dict__,
            "question": {**original.question, "stem": "수정된 1번 문항 ( )."},
        }
    )
    available = candidate(2)

    unused, used = unused_set_candidates(
        [revised, available], {original.source_key}
    )

    assert unused == [available]
    assert used == [revised]


def test_independent_item_drafts_do_not_require_a_complete_set():
    values = [candidate(1), candidate(2)]

    drafts = build_item_drafts(values, {}, 4, 0.0)

    assert len(drafts) == 2
    assert [value.candidate.slot for value in drafts] == [1, 2]
    assert all(value.metadata.target_level == 4 for value in drafts)


def test_listening_display_uses_dialogue_and_visual_descriptions():
    value = candidate(1, section="listening")
    value.question["choices"] = []
    value.question["visual_options"] = [
        {"number": number, "description": f"장면 {number}", "asset_path": f"assets/{number}.png"}
        for number in range(1, 5)
    ]

    assert "여자: 1번 대화입니다." in display_stem(value)
    assert display_choices(value) == ["장면 1", "장면 2", "장면 3", "장면 4"]


def test_collector_keeps_only_approved_valid_canonical_slots(tmp_path: Path):
    db_path = tmp_path / "data" / "types" / "grammar_blank.db"
    storage = Storage(db_path)
    result = ProviderResult(
        provider="model-key",
        model="exact-model",
        operation="generation",
        generation_preset="fast_draft",
        request_parameters={
            "api_parameters_applied": True,
            "reasoning_effort": "low",
            "max_completion_tokens": 32768,
        },
    )
    run_id = storage.save_run(result, "system", "user", "grammar_blank")
    base = {
        "question_type": "grammar_blank",
        "stem": "문장 ( ).",
        "choices": ["가", "나", "다", "라"],
        "answer": 1,
        "explanation": "해설",
        "target_grammar": "문법",
    }
    storage.add_generated_questions(
        run_id,
        "model-key",
        "exact-model",
        [{**base, "type_slot": 1}, {**base, "type_slot": 2}, {**base, "type_slot": 3}],
        [[], [{"code": "bad_structure", "message": "오류", "severity": "error"}], []],
    )
    for item in storage.list_generated():
        storage.save_review(item["id"], Review(approved=True))

    catalog = collect_candidate_catalog(tmp_path)
    grouped = group_by_slot(catalog.candidates)

    assert len(catalog.candidates) == 1
    assert catalog.candidates[0].generation_preset == "fast_draft"
    assert catalog.candidates[0].request_parameters["reasoning_effort"] == "low"
    assert len(grouped[1]) == 1
    assert len(catalog.exclusions) == 2
    assert {value.reason for value in catalog.exclusions} == {
        "자동 검사 오류가 남아 있습니다.",
        "grammar_blank 유형의 담당 번호 [1, 2]와 문항 번호 3이 일치하지 않습니다.",
    }


def test_postgres_source_provenance_includes_generation_preset():
    value = candidate(1)
    value = PublicationCandidate(
        **{
            **value.__dict__,
            "generation_preset": "precise_generation",
            "request_parameters": {
                "api_parameters_applied": True,
                "reasoning_effort": "high",
                "max_completion_tokens": 32768,
            },
        }
    )

    provenance = _source_provenance(value)

    assert provenance["generation_preset"] == "precise_generation"
    assert provenance["request_parameters"]["api_parameters_applied"] is True
    assert "api_key" not in provenance["request_parameters"]


def test_collector_discards_legacy_circled_choice_duplicate_error(tmp_path: Path):
    db_path = tmp_path / "data" / "types" / "sentence_insertion.db"
    storage = Storage(db_path)
    result = ProviderResult(provider="gpt_5_6_luna", model="gpt-5.6-luna", operation="generation")
    run_id = storage.save_run(result, "system", "user", "sentence_insertion")
    question = {
        "question_type": "sentence_insertion",
        "type_slot": 39,
        "stem": "삽입할 문장\n지문\n질문",
        "passage": "문장. (①) 문장. (②) 문장. (③) 문장. (④)",
        "auxiliary_text": "삽입할 문장",
        "question_prompt": "들어갈 곳을 고르십시오.",
        "choices": ["①", "②", "③", "④"],
        "answer": 2,
        "explanation": "해설",
        "target_grammar": "문장 삽입",
    }
    storage.add_generated_questions(
        run_id,
        "gpt_5_6_luna",
        "gpt-5.6-luna",
        [question],
        [[{"code": "duplicate_choices", "message": "서로 같은 보기가 있습니다.", "severity": "error"}]],
    )
    item = storage.list_generated()[0]
    storage.save_review(item["id"], Review(approved=True))

    catalog = collect_candidate_catalog(tmp_path)

    assert len(catalog.candidates) == 1
    assert catalog.candidates[0].slot == 39
    assert catalog.candidates[0].validation == []


def test_collector_normalizes_legacy_blank_spacing_and_clears_error(tmp_path: Path):
    db_path = tmp_path / "data" / "types" / "grammar_blank.db"
    storage = Storage(db_path)
    result = ProviderResult(provider="deepseek_v4_pro", model="deepseek-v4-pro", operation="generation")
    run_id = storage.save_run(result, "system", "user", "grammar_blank")
    question = {
        "question_type": "grammar_blank",
        "type_slot": 1,
        "stem": "내일은 일찍 (   ).",
        "choices": ["갑니다", "갔습니다", "가겠습니다", "가고 있습니다"],
        "answer": 3,
        "explanation": "미래 계획을 나타낸다.",
        "target_grammar": "-(으)ㄹ 것이다",
    }
    storage.add_generated_questions(
        run_id,
        "deepseek_v4_pro",
        "deepseek-v4-pro",
        [question],
        [[{"code": "blank_count", "message": "빈칸 '( )'이 정확히 한 개가 아닙니다.", "severity": "error"}]],
    )
    item = storage.list_generated()[0]
    storage.save_review(item["id"], Review(approved=True))

    catalog = collect_candidate_catalog(tmp_path)

    assert len(catalog.candidates) == 1
    assert catalog.candidates[0].question["stem"] == "내일은 일찍 ( )."
    assert catalog.candidates[0].validation == []


def test_migration_declares_required_item_bank_fields():
    sql = (Path(__file__).resolve().parents[1] / "topik_question_lab" / "migrations" / "001_question_bank.sql").read_text(encoding="utf-8")
    for field in [
        "item_id",
        "item_version",
        "section",
        "item_type",
        "primary_skill",
        "target_level",
        "predicted_difficulty",
        "irt_difficulty",
        "irt_discrimination",
        "stem_length",
        "choice_count",
        "generator_version",
        "review_status",
    ]:
        assert field in sql

    second = (Path(__file__).resolve().parents[1] / "topik_question_lab" / "migrations" / "002_complete_item_bank.sql").read_text(encoding="utf-8")
    assert "type_slot" in second
    assert "topik_bank.current_items" in second
    assert "topik_bank.current_set_contents" in second

    third = (Path(__file__).resolve().parents[1] / "topik_question_lab" / "migrations" / "003_multi_question_sets.sql").read_text(encoding="utf-8")
    assert "set_sequence" in third
    assert "question_sets_identity_sequence_key" in third
    assert "DROP CONSTRAINT IF EXISTS question_sets_section_generator_provider_generator_model_ge_key" in third

    fourth = (Path(__file__).resolve().parents[1] / "topik_question_lab" / "migrations" / "004_postgres_deployments.sql").read_text(encoding="utf-8")
    assert "topik_bank.deployment_runs" in fourth
    assert "topik_bank.deployment_run_sets" in fourth
    assert "outcome_unknown" in fourth


def test_reconciliation_reports_all_local_and_postgres_states():
    latest = candidate(1)
    unpublished = candidate(2)
    changed = candidate(3)
    ineligible = candidate(4)
    catalog = CandidateCatalog(
        candidates=[latest, unpublished, changed],
        records=[
            LocalPublicationRecord(latest, True),
            LocalPublicationRecord(unpublished, True),
            LocalPublicationRecord(changed, True),
            LocalPublicationRecord(ineligible, False, "최종 승인되지 않았습니다."),
        ],
    )
    published = [
        {
            "source_key": latest.source_key,
            "item_id": "item-1",
            "item_version": 1,
            "section": "reading",
            "generator_model": latest.provider_key,
            "generator_version": latest.model_id,
            "type_slot": 1,
            "item_type": latest.question_type,
            "created_at": "now",
            "content_json": latest.question,
        },
        {
            "source_key": changed.source_key,
            "item_id": "item-3",
            "item_version": 1,
            "section": "reading",
            "generator_model": changed.provider_key,
            "generator_version": changed.model_id,
            "type_slot": 3,
            "item_type": changed.question_type,
            "created_at": "now",
            "content_json": {**changed.question, "stem": "이전 문장 ( )."},
        },
        {
            "source_key": ineligible.source_key,
            "item_id": "item-4",
            "item_version": 1,
            "section": "reading",
            "generator_model": ineligible.provider_key,
            "generator_version": ineligible.model_id,
            "type_slot": 4,
            "item_type": ineligible.question_type,
            "created_at": "now",
            "content_json": ineligible.question,
        },
        {
            "source_key": "reading:removed:99",
            "item_id": "item-99",
            "item_version": 1,
            "section": "reading",
            "generator_model": "old",
            "generator_version": "old-model",
            "type_slot": 50,
            "item_type": "removed",
            "created_at": "now",
            "content_json": {},
        },
    ]

    rows = reconcile_catalog(catalog, published, {latest.source_key})
    statuses = {value["source_key"]: value["status"] for value in rows}

    assert statuses[latest.source_key] == "최신"
    assert statuses[unpublished.source_key] == "미발행"
    assert statuses[changed.source_key] == "수정 후 미동기"
    assert statuses[ineligible.source_key] == "발행 후 승인 취소/오류"
    assert statuses["reading:removed:99"] == "로컬 없음"
    assert next(value for value in rows if value["source_key"] == latest.source_key)["in_set"]


def test_postgres_error_redacts_connection_string_and_password():
    url = "postgresql://user:very-secret@localhost:5432/topik"
    message = _safe_error(RuntimeError(f"failed for {url}; password=very-secret"), url)
    assert url not in message
    assert "very-secret" not in message


def test_set_item_batch_insert_uses_cursor_executemany():
    class FakeCursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def executemany(self, sql, rows):
            self.calls.append((sql, rows))

    class FakeConnection:
        def __init__(self):
            self.value = FakeCursor()

        def cursor(self):
            return self.value

    connection = FakeConnection()
    rows = [("set", 1, 1, "item", 1)]

    _insert_set_items(connection, rows)

    assert len(connection.value.calls) == 1
    assert "question_set_items" in connection.value.calls[0][0]
    assert connection.value.calls[0][1] == rows


def test_set_membership_retry_is_idempotent_and_partial_overlap_conflicts():
    identity = ("reading", "chatkhu", "gpt_5_6_luna", "gpt-5.6-luna")
    requested = {slot: f"reading:type:{slot}" for slot in range(1, 51)}
    rows = [
        {
            "set_id": "set-one",
            "set_sequence": 1,
            "set_version": 1,
            "section": identity[0],
            "generator_provider": identity[1],
            "generator_model": identity[2],
            "generator_version": identity[3],
            "position": slot,
            "source_key": source_key,
        }
        for slot, source_key in requested.items()
    ]

    existing, conflicts = _resolve_set_membership(rows, requested, identity)
    assert existing == ("set-one", 1, 1)
    assert conflicts == []

    existing, conflicts = _resolve_set_membership(rows[:1], requested, identity)
    assert existing is None
    assert conflicts == ["reading:type:1"]


def test_set_membership_from_other_identity_is_always_a_conflict():
    requested = {1: "reading:type:1"}
    rows = [
        {
            "set_id": "other-set",
            "set_sequence": 1,
            "set_version": 1,
            "section": "reading",
            "generator_provider": "other",
            "generator_model": "other",
            "generator_version": "other",
            "position": 1,
            "source_key": "reading:type:1",
        }
    ]

    existing, conflicts = _resolve_set_membership(
        rows,
        requested,
        ("reading", "chatkhu", "gpt_5_6_luna", "gpt-5.6-luna"),
    )

    assert existing is None
    assert conflicts == ["reading:type:1"]
