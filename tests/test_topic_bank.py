import random
from collections import Counter

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.listening_prompts import build_listening_generation_prompt
from topik_question_lab.models import GeneratedQuestion, Review
from topik_question_lab.prompts import build_generation_prompt
from topik_question_lab.topic_bank import (
    SlotPoolConfigurationError,
    SlotTopicPool,
    TopicBrief,
    TopicStore,
    apply_topic_plan,
    generation_units,
    parse_pool_items,
    parse_slot_numbers,
    uses_topic_bank,
)
from topik_question_lab.topic_ui import planner_rows


def test_default_bank_seeds_once_and_restore_does_not_overwrite_edits(tmp_path):
    path = tmp_path / "topic_bank.db"
    store = TopicStore(path)

    topics = store.list_topics()
    assert len(topics) == 120
    assert set(Counter(topic.domain for topic in topics).values()) == {10}
    assert all(len(topic.angles) == 3 for topic in topics)

    first = topics[0]
    store.save_topic(first.model_copy(update={"title": "사용자가 수정한 소재"}))
    assert store.seed_defaults() == 0
    assert store.get_topic(first.id).title == "사용자가 수정한 소재"

    missing = topics[-1]
    with store.connect() as connection:
        connection.execute("DELETE FROM topics WHERE id = ?", (missing.id,))
    assert store.seed_defaults() == 1
    assert store.get_topic(missing.id) is not None
    assert store.get_topic(first.id).title == "사용자가 수정한 소재"


def test_default_slot_pools_seed_exactly_and_restore_without_overwrite(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    pools = store.list_slot_pools()
    assert len(pools) == 8
    assert {
        (pool.section, tuple(pool.slot_numbers)): len(pool.items)
        for pool in pools
    } == {
        ("reading", (5,)): 50,
        ("reading", (6,)): 41,
        ("reading", (7,)): 24,
        ("reading", (8,)): 21,
        ("reading", (11, 12)): 8,
        ("reading", (25, 26, 27)): 9,
        ("reading", (44, 45, 46, 47, 48, 49, 50)): 10,
        ("listening", (41, 42, 43, 44, 45, 46, 47, 48, 49, 50)): 10,
    }
    reading_five = next(pool for pool in pools if pool.slot_numbers == [5])
    assert "책(소설, 시집)" in reading_five.items
    assert store.seed_default_slot_pools() == 0

    edited = reading_five.model_copy(update={"name": "사용자가 수정한 이름"})
    store.save_slot_pool(edited)
    missing = pools[-1]
    with store.connect() as connection:
        connection.execute("DELETE FROM slot_topic_pools WHERE id = ?", (missing.id,))
    assert store.seed_default_slot_pools() == 1
    assert store.get_slot_pool(reading_five.id).name == "사용자가 수정한 이름"
    assert store.get_slot_pool(missing.id) is not None


def test_slot_pool_input_parsers_preserve_parenthesized_commas():
    assert parse_pool_items("시계, 책(소설, 시집)\n우산, 시계,  ") == [
        "시계",
        "책(소설, 시집)",
        "우산",
    ]
    assert parse_slot_numbers("11 ~ 12, 15번\n17-18") == [11, 12, 15, 17, 18]


def test_slot_pool_precedence_and_mixed_type_plan(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    units = generation_units((9, 10, 11, 12), 4, False)
    plan = store.select_plan(
        section="reading",
        question_type="content_match_short",
        units=units,
        rng=random.Random(17),
    )
    assert [brief.slot_numbers for brief in plan] == [[9], [10], [11], [12]]
    assert [brief.source for brief in plan] == ["global", "global", "slot_pool", "slot_pool"]
    assert all(brief.pool_id == "default_reading_11_12_information" for brief in plan[2:])

    store.set_slot_pool_active("default_reading_11_12_information", False)
    fallback = store.select_plan(
        section="reading",
        question_type="content_match_short",
        units=units,
        rng=random.Random(17),
    )
    assert all(brief.source == "global" for brief in fallback)


def test_explicit_pool_overrides_topic_free_type_and_partial_shared_pool_is_rejected(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    store.save_slot_pool(
        SlotTopicPool(
            id="custom_easy",
            section="reading",
            slot_numbers=[1],
            name="읽기 1번 직접 지정",
            prompt_instruction="선정 소재를 문법 문장의 상황으로 사용합니다.",
            items=["우산", "약속"],
        )
    )
    plan = store.select_plan(
        section="reading",
        question_type="grammar_blank",
        units=generation_units((1, 2), 2, False),
        rng=random.Random(3),
    )
    assert len(plan) == 1
    assert plan[0].slot_numbers == [1]
    assert plan[0].source == "slot_pool"

    store.set_slot_pool_active("default_reading_44_50_advanced", False)
    store.save_slot_pool(
        SlotTopicPool(
            id="partial_shared",
            section="reading",
            slot_numbers=[44],
            name="불완전 세트",
            prompt_instruction="테스트 지침",
            items=["역사"],
        )
    )
    with pytest.raises(SlotPoolConfigurationError, match="모든 문항"):
        store.select_plan(
            section="reading",
            question_type="paired_44_45",
            units=generation_units((44, 45), 2, True),
        )


def test_slot_pool_overlap_validation_and_balanced_shuffle_cycle(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    pool = SlotTopicPool(
        id="cycle_pool",
        section="reading",
        slot_numbers=[13],
        name="순환 테스트",
        prompt_instruction="테스트 지침",
        items=["가", "나", "다"],
    )
    store.save_slot_pool(pool)
    with pytest.raises(SlotPoolConfigurationError, match="포함"):
        store.save_slot_pool(
            SlotTopicPool(
                id="overlap_pool",
                section="reading",
                slot_numbers=[13, 14],
                name="중복 테스트",
                prompt_instruction="테스트 지침",
                items=["라"],
            )
        )

    units = [(f"item-{index}", [13]) for index in range(1, 8)]
    plan = store.select_plan(
        section="reading",
        question_type="sentence_order",
        units=units,
        rng=random.Random(21),
    )
    assert len({brief.title for brief in plan[:3]}) == 3
    counts = Counter(brief.title for brief in plan)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_slot_pool_usage_is_recorded_separately_and_affects_next_pick(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    first = store.select_plan(
        section="reading",
        question_type="short_text_topic",
        units=[("item-1", [5])],
        rng=random.Random(5),
    )
    store.record_usages(
        first,
        section="reading",
        question_type="short_text_topic",
        provider="model-a",
        model="model-a",
        run_id=1,
    )
    assert store.list_usages() == []
    assert store.list_slot_pool_usages()[0]["item"] == first[0].title

    second = store.select_plan(
        section="reading",
        question_type="short_text_topic",
        units=[("item-1", [5])],
        rng=random.Random(5),
    )
    assert second[0].title != first[0].title


def test_slot_pool_avoids_other_model_assignments_until_pool_is_exhausted(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    units = generation_units((11, 12), 2, False)
    first = store.select_plan(
        section="reading",
        question_type="content_match_short",
        units=units,
        rng=random.Random(31),
    )
    second = store.select_plan(
        section="reading",
        question_type="content_match_short",
        units=units,
        forbidden_pairs={brief.pair for brief in first},
        rng=random.Random(32),
    )
    assert not ({brief.topic_id for brief in first} & {brief.topic_id for brief in second})

    reserved: Counter[tuple[str, str]] = Counter()
    selected_titles: list[str] = []
    for index in range(17):
        current = store.select_plan(
            section="reading",
            question_type="content_match_short",
            units=[(f"item-{index}", [11])],
            forbidden_pairs=set(reserved),
            initial_pair_counts=reserved,
            rng=random.Random(index),
        )[0]
        reserved[current.pair] += 1
        selected_titles.append(current.title)
    cycle_counts = Counter(selected_titles)
    assert len(cycle_counts) == 8
    assert max(cycle_counts.values()) - min(cycle_counts.values()) <= 1


def test_custom_pool_prompt_contains_only_selected_item_and_pool_instruction():
    brief = TopicBrief(
        unit_key="item-1",
        slot_numbers=[5],
        topic_id="slot_pool:test:one",
        domain="읽기 5번 물품",
        title="우산",
        angle="번호별 지정 소재",
        source="slot_pool",
        pool_id="test",
        prompt_instruction="용도와 특징으로 물품을 추론하게 합니다.",
    )
    prompt = build_generation_prompt([], "분석서", 1, "초급", "short_text_topic", [brief])
    assert "우산" in prompt
    assert "용도와 특징으로 물품을 추론하게 합니다." in prompt
    assert "시계" not in prompt
    assert "slot_pool:test:one" in prompt


def test_apply_topic_plan_leaves_intentionally_unplanned_easy_slot_untouched():
    brief = TopicBrief(
        unit_key="item-1",
        slot_numbers=[1],
        topic_id="slot_pool:test:easy",
        domain="읽기 1번 직접 지정",
        title="우산",
        angle="번호별 지정 소재",
        source="slot_pool",
        pool_id="test",
    )
    applied = apply_topic_plan(
        {
            "questions": [
                {"type_slot": 2, "topic_domain": "임의 분야", "topic_title": "임의 소재"},
                {"type_slot": 1},
            ]
        },
        [brief],
        shared=False,
    )
    assert all(
        applied.payload["questions"][0][field] == ""
        for field in ("topic_id", "topic_domain", "topic_title", "topic_angle")
    )
    assert applied.payload["questions"][1]["topic_id"] == brief.topic_id


def test_apply_topic_plan_clears_unsolicited_metadata_when_no_topic_is_assigned():
    applied = apply_topic_plan(
        {
            "questions": [
                {
                    "type_slot": 3,
                    "topic_id": "",
                    "topic_domain": "교통·생활",
                    "topic_title": "공유 자전거 이용 목적",
                    "topic_angle": "이용 목적별 비율과 순위",
                }
            ]
        },
        [],
        shared=False,
    )
    assert applied.payload["questions"][0] == {
        "type_slot": 3,
        "topic_id": "",
        "topic_domain": "",
        "topic_title": "",
        "topic_angle": "",
    }


def test_selection_is_balanced_compatible_and_respects_global_recent_pairs(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    units = generation_units((35, 36, 37, 38), 12, False)
    plan = store.select_plan(
        section="reading",
        question_type="main_topic",
        units=units,
        rng=random.Random(7),
    )

    assert len(plan) == 12
    assert len({brief.topic_id for brief in plan}) == 12
    assert len({brief.domain for brief in plan}) == 12

    store.record_usages(
        plan,
        section="reading",
        question_type="main_topic",
        provider="model-a",
        model="model-a",
        run_id=1,
    )
    listening_plan = store.select_plan(
        section="listening",
        question_type="paired_41_42",
        units=generation_units((41, 42), 24, True),
        rng=random.Random(9),
    )
    assert not ({brief.pair for brief in plan} & {brief.pair for brief in listening_plan})

    conversational = store.select_plan(
        section="listening",
        question_type="paired_21_22",
        units=generation_units((21, 22), 20, True),
        rng=random.Random(11),
    )
    assert {brief.domain for brief in conversational}.isdisjoint(
        {"경제·산업", "과학·기술", "자연·생태", "역사·언어"}
    )


def test_easy_types_skip_topics_and_material_types_use_type_specific_pools(tmp_path):
    assert not uses_topic_bank("reading", "grammar_blank")
    assert not uses_topic_bank("reading", "similar_expression")
    assert uses_topic_bank("reading", "short_text_topic")
    assert not uses_topic_bank("listening", "visual_scene")
    assert not uses_topic_bank("listening", "main_idea_once")
    assert uses_topic_bank("listening", "paired_21_22")

    store = TopicStore(tmp_path / "topic_bank.db")
    science = next(topic for topic in store.list_topics() if topic.domain == "과학·기술")
    daily = next(topic for topic in store.list_topics() if topic.domain == "생활·소비")
    assert "short_text_topic" not in science.reading_types
    assert "paired_42_43" not in science.reading_types
    assert "main_topic" in science.reading_types
    assert "short_text_topic" in daily.reading_types
    assert "paired_42_43" in daily.reading_types
    assert "paired_21_22" not in science.listening_types
    assert "paired_41_42" in science.listening_types
    assert "paired_21_22" in daily.listening_types


def test_topic_plan_applies_to_independent_items_and_shared_sets():
    independent = [
        TopicBrief(unit_key="item-1", slot_numbers=[35], topic_id="a", domain="과학", title="종이", angle="보존"),
        TopicBrief(unit_key="item-2", slot_numbers=[36], topic_id="b", domain="문화", title="무대", angle="소품"),
    ]
    applied = apply_topic_plan(
        {"questions": [{"type_slot": 36}, {"type_slot": 35}]},
        independent,
        shared=False,
    )
    assert [question["topic_id"] for question in applied.payload["questions"]] == ["b", "a"]
    assert len(applied.applied_briefs) == 2

    shared = [
        TopicBrief(unit_key="set-1", slot_numbers=[44, 45], topic_id="c", domain="역사", title="우편", angle="주소"),
        TopicBrief(unit_key="set-2", slot_numbers=[44, 45], topic_id="d", domain="생태", title="갯벌", angle="물길"),
    ]
    shared_payload = {
        "questions": [
            {"type_slot": 44, "set_id": "set-2"},
            {"type_slot": 45, "set_id": "set-2"},
            {"type_slot": 44, "set_id": "set-1"},
            {"type_slot": 45, "set_id": "set-1"},
        ]
    }
    shared_applied = apply_topic_plan(shared_payload, shared, shared=True)
    assert [question["topic_id"] for question in shared_applied.payload["questions"]] == ["d", "d", "c", "c"]


def test_generation_prompts_include_only_assigned_topics():
    brief = TopicBrief(
        unit_key="item-1",
        slot_numbers=[35],
        topic_id="topic_test",
        domain="과학·기술",
        title="오래된 종이의 산성화",
        angle="보관 온도의 영향",
    )
    reading = build_generation_prompt([], "분석서", 1, "고급", "main_topic", [brief])
    listening = build_listening_generation_prompt([], "분석서", 1, "고급", "content_match_once", [brief])

    for prompt in (reading, listening):
        assert "이번 생성의 소재 배정표" in prompt
        assert "topic_test" in prompt
        assert "오래된 종이의 산성화" in prompt
        assert "다른 행의 소재를 섞지 않습니다" in prompt


def test_legacy_generated_models_and_reviews_remain_compatible():
    question = GeneratedQuestion(
        type_slot=1,
        stem="비가 오면 우산을 ( ) 좋다.",
        choices=["챙기면", "챙기지만", "챙기거나", "챙기도록"],
        answer=1,
        explanation="조건에 맞는다.",
        target_grammar="조건",
    )
    review = Review()

    assert question.topic_id == ""
    assert question.topic_angle == ""
    assert review.topic_fit == 3


def test_topic_bank_page_renders():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    app = AppTest.from_file(str(root / "topik_question_lab" / "topic_bank_app.py"), default_timeout=20).run()
    assert not app.exception
    assert any(title.value == "소재 은행" for title in app.title)
    assert any(metric.label == "전체 소재" and metric.value == "120" for metric in app.metric)
    assert [tab.label for tab in app.tabs] == ["공용 소재 은행", "문항별 지정 풀"]
    assert any(button.label == "지정 풀 저장" for button in app.button)
    assert any(button.label == "새 지정 풀 저장" for button in app.button)

    coverage = next(
        dataframe.value
        for dataframe in app.dataframe
        if list(dataframe.value.columns) == ["문항 번호", "상태", "적용 풀", "항목 수"]
    )
    assert {13, 16}.issubset(set(coverage["문항 번호"]))


def test_planner_rows_keep_slots_without_a_custom_pool_visible():
    units = [(f"item-{index}", [slot]) for index, slot in enumerate(range(13, 17), start=1)]
    plans = {
        "provider": [
            TopicBrief(
                unit_key="item-2",
                slot_numbers=[14],
                topic_id="slot_pool_14:item",
                domain="듣기 14번",
                title="안내 방송",
                angle="안내 방송",
                source="slot_pool",
                pool_id="slot_pool_14",
            ),
            TopicBrief(
                unit_key="item-3",
                slot_numbers=[15],
                topic_id="slot_pool_15:item",
                domain="듣기 15번",
                title="뉴스",
                angle="뉴스",
                source="slot_pool",
                pool_id="slot_pool_15",
            ),
        ]
    }

    rows, selectors = planner_rows(plans, ["provider"], units)

    assert [row["문항 슬롯"] for row in rows] == ["13", "14", "15", "16"]
    assert [rows[index]["소재"] for index in (0, 3)] == ["소재 미지정", "소재 미지정"]
    assert [rows[index]["출처"] for index in (0, 3)] == ["기존 방식", "기존 방식"]
    assert [selectors[index][1] for index in range(4)] == [None, 0, 1, None]
