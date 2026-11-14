import random

import pytest

from topik_question_lab.common.generation import topic_bank
from topik_question_lab.common.generation.exam_profiles import profiles_for_exam
from topik_question_lab.common.generation.listening_prompts import build_listening_generation_prompt
from topik_question_lab.common.generation.slot_topic_catalog import default_slot_pool_records
from topik_question_lab.common.generation.topic_bank import (
    SlotTopicPool,
    TopicStore,
    apply_topic_plan,
    generation_units,
)


NEW_SLOTS = {3, 13, *range(16, 41)}
EXISTING_CUSTOM_SLOTS = [(1, 2), (4, 5, 6, 7, 8), (9, 10, 11, 12), (14,), (15,)]


def database_rows(store):
    with store.connect() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("topics", "topic_usages", "slot_topic_pools", "slot_pool_usages", "generation_usage_receipts")
        }


@pytest.mark.parametrize("custom_overlap", [False, True])
def test_existing_database_only_gains_missing_listening_pools(tmp_path, monkeypatch, custom_overlap):
    legacy_records = [
        raw for raw in default_slot_pool_records()
        if raw["section"] == "reading" or min(raw["slot_numbers"]) >= 41
    ]
    path = tmp_path / "topic_bank.db"
    with monkeypatch.context() as legacy:
        legacy.setattr(topic_bank, "default_slot_pool_records", lambda: legacy_records)
        store = TopicStore(path)
    slots = EXISTING_CUSTOM_SLOTS + ([(33, 34)] if custom_overlap else [])
    for numbers in slots:
        store.save_slot_pool(SlotTopicPool(
            id=f"custom_{numbers[0]}", section="listening", slot_numbers=list(numbers),
            name="기존 사용자 지정 풀", prompt_instruction="기존 지침을 유지합니다.",
            items=["기존 소재 하나", "기존 소재 둘"],
        ))

    # Include historical selections from all three sources in the preservation check.
    for section, type_id, numbers in (
        ("reading", "sentence_insertion", [41]),
        ("listening", "visual_scene", [1]),
        ("listening", "paired_41_42", [41, 42]),
    ):
        plan = store.select_plan(section=section, question_type=type_id, units=[("unit-1", numbers)])
        store.record_usages(plan, section=section, question_type=type_id, provider="test", model="test", run_id=1)
    before = database_rows(store)

    store = TopicStore(path)
    after = database_rows(store)
    for table, rows in before.items():
        if table == "slot_topic_pools":
            original_ids = {row[0] for row in rows}
            assert [row for row in after[table] if row[0] in original_ids] == rows
            assert len(after[table]) == len(rows) + 14
        else:
            assert after[table] == rows
    new_pools = [pool for pool in store.list_slot_pools() if pool.id not in original_ids]
    assert {slot for pool in new_pools for slot in pool.slot_numbers} == NEW_SLOTS
    active_slots = [slot for pool in store.list_slot_pools(section="listening", active_only=True) for slot in pool.slot_numbers]
    assert sorted(active_slots) == list(range(1, 51))
    if custom_overlap:
        plan = store.select_plan(section="listening", question_type="paired_33_34", units=[("set-1", [33, 34])])
        assert plan[0].pool_id == "custom_33"
        assert not store.get_slot_pool("default_listening_33_34_culture_information").active
    assert store.seed_default_slot_pools() == 0
    assert database_rows(store) == after


def test_new_default_edits_and_disabled_state_survive_reopening(tmp_path):
    path = tmp_path / "topic_bank.db"
    store = TopicStore(path)
    pool = store.get_slot_pool("default_listening_13_daily_information")
    store.save_slot_pool(pool.model_copy(update={"items": ["사용자가 수정한 소재"], "active": False}))
    before = database_rows(store)
    assert database_rows(TopicStore(path)) == before


@pytest.mark.parametrize("exam_format", ["pbt", "ibt"])
def test_listening_defaults_reach_matching_slots_and_shared_scripts(tmp_path, exam_format):
    store = TopicStore(tmp_path / "topic_bank.db")
    assigned_slots = set()
    for type_id, profile in profiles_for_exam("listening", exam_format).items():
        units = generation_units(profile.question_numbers, len(profile.question_numbers), profile.shared_script)
        plan = store.select_plan(section="listening", question_type=type_id, units=units, rng=random.Random(9))
        assigned_slots.update(slot for brief in plan for slot in brief.slot_numbers)
        semantic_slots = profile.topic_slots if exam_format == "ibt" else profile.question_numbers
        expected_slots = {
            slot for slot, reference in zip(profile.question_numbers, semantic_slots)
            if reference in NEW_SLOTS or reference >= 41
        }
        assert {slot for brief in plan for slot in brief.slot_numbers} == expected_slots
        assert all(brief.source == "slot_pool" for brief in plan)
        if exam_format == "ibt":
            slot_map = dict(zip(profile.question_numbers, profile.topic_slots))
            pbt_plan = store.select_plan(
                section="listening", question_type=profile.base_type_id,
                units=[(key, [slot_map[n] for n in numbers]) for key, numbers in units], rng=random.Random(9),
            )
            assert [(brief.pool_id, brief.title) for brief in plan] == [(brief.pool_id, brief.title) for brief in pbt_plan]
        if not plan:
            continue
        prompt = build_listening_generation_prompt([], "분석서", len(profile.question_numbers), "중고급", type_id, plan)
        for brief in plan:
            assert brief.title in prompt
            assert brief.prompt_instruction in prompt
        if profile.shared_script:
            assert len(plan) == 1
            payload = {"questions": [{"set_id": plan[0].unit_key, "type_slot": slot} for slot in profile.question_numbers]}
            applied = apply_topic_plan(payload, plan, shared=True)
            assert not any(applied.issue_messages)
            assert {question["topic_id"] for question in applied.payload["questions"]} == {plan[0].topic_id}
    expected = NEW_SLOTS | set(range(41, 51)) if exam_format == "pbt" else {2, 9, *range(12, 31)}
    assert assigned_slots == expected
