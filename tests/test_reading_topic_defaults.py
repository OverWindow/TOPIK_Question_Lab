import random
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.generation import topic_bank
from topik_question_lab.common.generation.exam_profiles import profiles_for_exam
from topik_question_lab.common.generation.prompts import build_generation_prompt
from topik_question_lab.common.generation.slot_topic_catalog import default_slot_pool_records
from topik_question_lab.common.generation.topic_bank import SlotTopicPool, TopicStore, apply_topic_plan, generation_units
from topik_question_lab.menus.topic_bank.topic_browser import applications_for_exam


NEW_SLOTS = {9, 10, *range(16, 25), *range(28, 41)}
NEW_IDS = {
    "default_reading_09_notices", "default_reading_10_surveys",
    "default_reading_16_18_daily_explanations", "default_reading_19_20_information",
    "default_reading_21_22_daily_context", "default_reading_23_24_personal_narratives",
    "default_reading_28_31_explanatory_arguments", "default_reading_32_34_reports",
    "default_reading_35_38_social_arguments", "default_reading_39_40_cohesive_explanations",
}
PAGE = Path(__file__).resolve().parents[1] / "topik_question_lab/menus/topic_bank/topic_bank_app.py"


def database_rows(store):
    with store.connect() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("topics", "topic_usages", "slot_topic_pools", "slot_pool_usages", "generation_usage_receipts")
        }


def protected_sources(store):
    return [
        (exam, application.slots, application.resolved)
        for exam in ("pbt", "ibt")
        for application in applications_for_exam(store, "reading", exam)
        if set(application.reference_slots).isdisjoint(NEW_SLOTS)
    ]


@pytest.mark.parametrize("custom_overlap", [False, True])
def test_reading_upgrade_preserves_existing_materials_and_history(tmp_path, monkeypatch, custom_overlap):
    legacy_records = [raw for raw in default_slot_pool_records() if raw["id"] not in NEW_IDS]
    path = tmp_path / "topic_bank.db"
    with monkeypatch.context() as legacy:
        legacy.setattr(topic_bank, "default_slot_pool_records", lambda: legacy_records)
        store = TopicStore(path)
    for slots in ([13, 14, 15], *([[35, 36, 37, 38]] if custom_overlap else [])):
        store.save_slot_pool(SlotTopicPool(
            id=f"custom_{slots[0]}", section="reading", slot_numbers=slots,
            name="기존 지정 소재", prompt_instruction="사용자 지정 지침을 유지합니다.",
            items=["기존 소재 하나", "기존 소재 둘"],
        ))
    old_pool = store.get_slot_pool("default_reading_44_50_advanced")
    store.save_slot_pool(old_pool.model_copy(update={"items": ["사용자가 수정한 고급 소재"]}))
    for section, type_id, slots in (
        ("reading", "content_match_short", [9]),
        ("reading", "sentence_order", [13]),
        ("reading", "paired_44_45", [44, 45]),
        ("listening", "paired_41_42", [41, 42]),
    ):
        plan = store.select_plan(section=section, question_type=type_id, units=[("unit-1", slots)])
        store.record_usages(
            plan, section=section, question_type=type_id, provider="test", model="test", run_id=1,
            usage_key=f"{section}-{type_id}-1",
        )
    before = database_rows(store)
    assert all(before[table] for table in ("topic_usages", "slot_pool_usages", "generation_usage_receipts"))
    protected = protected_sources(store)

    store = TopicStore(path)
    after = database_rows(store)
    original_ids = {row[0] for row in before["slot_topic_pools"]}
    for table, rows in before.items():
        if table == "slot_topic_pools":
            assert [row for row in after[table] if row[0] in original_ids] == rows
            assert {row[0] for row in after[table]} - original_ids == NEW_IDS
        else:
            assert after[table] == rows
    assert protected_sources(store) == protected
    assert {slot for pool in store.list_slot_pools() if pool.id in NEW_IDS for slot in pool.slot_numbers} == NEW_SLOTS
    active_slots = [slot for pool in store.list_slot_pools(section="reading", active_only=True) for slot in pool.slot_numbers]
    assert len(active_slots) == len(set(active_slots))
    assert set(active_slots) == {*range(5, 41), *range(44, 51)}
    if custom_overlap:
        assert store.resolve_source("reading", "main_topic", [35]).pool.id == "custom_35"
        assert not store.get_slot_pool("default_reading_35_38_social_arguments").active
    assert store.seed_default_slot_pools() == 0
    assert database_rows(store) == after


def test_reading_pool_edits_and_disabled_state_survive_reopening(tmp_path):
    path = tmp_path / "topic_bank.db"
    store = TopicStore(path)
    pool = store.get_slot_pool("default_reading_10_surveys")
    store.save_slot_pool(pool.model_copy(update={"items": ["사용자가 편집한 조사 소재"], "active": False}))
    before = database_rows(store)
    reopened = TopicStore(path)
    assert database_rows(reopened) == before
    assert reopened.resolve_source("reading", "ibt_chart_content_match", [7]).source == "global"


@pytest.mark.parametrize("exam", ["pbt", "ibt"])
def test_reading_defaults_reach_exact_slots_prompts_and_shared_passages(tmp_path, exam):
    store = TopicStore(tmp_path / "topic_bank.db")
    assigned = set()
    for type_id, profile in profiles_for_exam("reading", exam).items():
        units = generation_units(profile.question_numbers, len(profile.question_numbers), profile.shared_passage)
        plan = store.select_plan(section="reading", question_type=type_id, units=units, rng=random.Random(7))
        refs = profile.topic_slots if exam == "ibt" else profile.question_numbers
        expected = {n for n, ref in zip(profile.question_numbers, refs) if ref in NEW_SLOTS}
        actual = {n for brief in plan if brief.pool_id in NEW_IDS for n in brief.slot_numbers}
        assert actual == expected
        assigned.update(actual)
        if not expected:
            continue
        prompt = build_generation_prompt([], "분석서", len(profile.question_numbers), "중고급", type_id, plan)
        for brief in plan:
            assert brief.title in prompt
            if brief.pool_id in NEW_IDS:
                assert brief.prompt_instruction in prompt
                assert brief.title in store.get_slot_pool(brief.pool_id).items
        if exam == "ibt":
            slot_map = dict(zip(profile.question_numbers, refs))
            pbt_plan = store.select_plan(
                section="reading", question_type=profile.base_type_id,
                units=[(key, [slot_map[n] for n in numbers]) for key, numbers in units], rng=random.Random(7),
            )
            assert [(brief.pool_id, brief.title) for brief in plan] == [(brief.pool_id, brief.title) for brief in pbt_plan]
        if profile.shared_passage:
            assert len(plan) == 1
            payload = {"questions": [{"set_id": plan[0].unit_key, "type_slot": slot} for slot in profile.question_numbers]}
            applied = apply_topic_plan(payload, plan, shared=True)
            assert not any(applied.issue_messages)
            assert {q["topic_id"] for q in applied.payload["questions"]} == {plan[0].topic_id}
    assert assigned == (NEW_SLOTS if exam == "pbt" else {7, 10, 11, 12, *range(15, 25)})


@pytest.mark.parametrize(("exam", "slot"), [("pbt", "10"), ("ibt", "7")])
def test_new_reading_pool_is_visible_and_editable_in_unified_page(tmp_path, exam, slot):
    store = TopicStore(tmp_path / "data/topic_bank.db")
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.radio(key="topic-section").set_value("reading").run()
    app.radio(key="topic-exam").set_value(exam).run()
    app.text_input(key="topic-query").set_value(slot).run()
    assert not app.exception
    assert any(header.value == "읽기 10번 생활 조사·도표" for header in app.subheader)
    assert any("PBT 10 / IBT 7" in caption.value for caption in app.caption)
    field = app.text_area(key="pool-items-default_reading_10_surveys")
    assert len(field.value.splitlines()) == 80
    field.set_value(field.value + "\n새 조사 소재")
    next(button for button in app.button if button.label == "소재 풀 저장").click().run()
    assert not app.exception
    assert store.get_slot_pool("default_reading_10_surveys").items[-1] == "새 조사 소재"
