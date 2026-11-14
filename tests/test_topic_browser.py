import random
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.generation.topic_bank import SlotTopicPool, TopicStore
from topik_question_lab.menus.topic_bank.topic_browser import application_matches, applications_for_exam, pool_exam_slots


PAGE = Path(__file__).resolve().parents[1] / "topik_question_lab/menus/topic_bank/topic_bank_app.py"


def snapshot(store):
    with store.connect() as connection:
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("topics", "topic_usages", "slot_topic_pools", "slot_pool_usages", "generation_usage_receipts")}


@pytest.mark.parametrize("section", ["reading", "listening"])
@pytest.mark.parametrize("exam", ["pbt", "ibt"])
def test_inspection_and_generation_resolve_the_same_sources(tmp_path, section, exam):
    store = TopicStore(tmp_path / "topic_bank.db")
    before = snapshot(store)
    applications = applications_for_exam(store, section, exam)
    assert sorted(n for application in applications for n in application.slots) == list(range(1, 31 if exam == "ibt" else 51))
    for application in applications:
        assert not application.error
        source = application.resolved
        plan = store.select_plan(section=section, question_type=application.question_type,
                                 units=[("unit", application.slots)], rng=random.Random(8))
        if source.source == "none":
            assert plan == []
            continue
        assert len(plan) == 1
        assert plan[0].source == source.source
        assert plan[0].slot_numbers == application.slots
        if source.pool:
            assert plan[0].pool_id == source.pool.id
            assert plan[0].title in source.pool.items
            assert set(application.slots) <= set(pool_exam_slots(source.pool, exam))
        else:
            assert plan[0].topic_id in {topic.id for topic in source.topics}
    assert snapshot(store) == before


def test_inactive_pool_and_invalid_shared_assignment_are_visible(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    store.set_slot_pool_active("default_listening_21_22_social_opinions", False)
    source = store.resolve_source("listening", "ibt_idea_content", [15, 16])
    assert source.source == "global" and source.topics
    store.save_slot_pool(SlotTopicPool(id="partial", section="listening", slot_numbers=[21], name="불완전 연결",
                                       prompt_instruction="테스트 지침", items=["기존 소재"]))
    application = next(a for a in applications_for_exam(store, "listening", "ibt") if a.slots == [15, 16])
    assert application.resolved is None
    assert "모든 문항" in application.error
    assert application.row()["적용 풀"] == "설정 확인 필요"


def test_number_search_uses_the_selected_exam_not_numbers_in_pool_names(tmp_path):
    store = TopicStore(tmp_path / "topic_bank.db")
    applications = applications_for_exam(store, "listening", "ibt")
    matches = [a for a in applications if application_matches(a, "20번")]
    assert [a.slots for a in matches] == [[19, 20]]
    assert matches[0].resolved.pool.slot_numbers == [27, 28]


def test_exam_row_selection_opens_the_actual_pool_and_save_preserves_other_data(tmp_path):
    store = TopicStore(tmp_path / "data/topic_bank.db")
    plan = store.select_plan(section="listening", question_type="visual_chart", units=[("item-1", [3])])
    store.record_usages(plan, section="listening", question_type="visual_chart", provider="test", model="test", run_id=1)
    before = snapshot(store)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.radio(key="topic-exam").set_value("ibt").run()
    app.session_state["applications-listening-ibt-"] = {"selection": {"rows": [1], "columns": []}}
    app.run()
    assert not app.exception
    assert any(header.value == "듣기 3번 생활 조사·그래프" for header in app.subheader)
    assert any("PBT 3 / IBT 2" in caption.value for caption in app.caption)
    # AppTest does not send dataframe selections back with other widget events.
    app.text_input(key="topic-query").set_value("생활 조사·그래프").run()
    app.text_input(key="pool-search-default_listening_03_surveys").set_value("운동").run()
    full_list = next(area for area in app.text_area if area.label == "전체 소재 목록")
    assert len(full_list.value.splitlines()) == 80
    full_list.set_value(full_list.value + "\n새 운동 조사")
    next(button for button in app.button if button.label == "소재 풀 저장").click().run()
    assert not app.exception
    after = snapshot(store)
    for table, rows in before.items():
        if table == "slot_topic_pools":
            assert [row for row in after[table] if row[0] != plan[0].pool_id] == [row for row in rows if row[0] != plan[0].pool_id]
        else:
            assert after[table] == rows
    assert store.get_slot_pool(plan[0].pool_id).items[-1] == "새 운동 조사"
    assert any("저장했습니다" in message.value for message in app.success)


def test_global_topics_are_filtered_by_type_and_editable_from_the_same_screen(tmp_path):
    store = TopicStore(tmp_path / "data/topic_bank.db")
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.radio(key="topic-section").set_value("reading").run()
    # Select a global-source row by its displayed pool, independent of the profile label.
    app.text_input(key="topic-query").set_value("공용 소재").run()
    assert not app.exception
    assert any(header.value == "공용 소재" for header in app.subheader)
    selected_title = next(field for field in app.text_input if field.label == "소재명").value
    topic = next(topic for topic in store.list_topics() if topic.title == selected_title)
    next(field for field in app.text_input if field.label == "소재명").set_value("수정한 공용 소재")
    next(button for button in app.button if button.label == "공용 소재 저장").click().run()
    assert not app.exception
    assert store.get_topic(topic.id).title == "수정한 공용 소재"
    app.radio(key="topic-view").set_value("전체 소재 풀").run()
    assert not app.exception
    assert any(header.value == "공용 소재" for header in app.subheader)


def test_all_pool_view_includes_disabled_pools_and_rejects_empty_items(tmp_path):
    store = TopicStore(tmp_path / "data/topic_bank.db")
    pool_id = "default_listening_13_daily_information"
    store.set_slot_pool_active(pool_id, False)
    before = snapshot(store)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.radio(key="topic-view").set_value("전체 소재 풀").run()
    app.radio(key="topic-exam").set_value("ibt").run()
    app.text_input(key="topic-query").set_value("생활 정보 대화").run()
    assert not app.exception
    assert any(header.value == "듣기 13번 생활 정보 대화" for header in app.subheader)
    next(area for area in app.text_area if area.label == "전체 소재 목록").set_value("")
    next(button for button in app.button if button.label == "소재 풀 저장").click().run()
    assert not app.exception and app.error
    assert snapshot(store) == before
