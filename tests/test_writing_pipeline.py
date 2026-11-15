import json
import random
import sqlite3
from copy import deepcopy

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.generation.providers import manual_result
from topik_question_lab.common.generation.topic_bank import SlotTopicPool, TopicBrief, TopicStore, slot_item_topic_id
from topik_question_lab.common.generation.writing_topic_catalog import WRITING_POOL_ID, split_writing_topic
from topik_question_lab.common.models import ProviderResult
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.writing_models import PROFILES, WritingPayload, WritingQuestion
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing import writing_pipeline as pipeline
from topik_question_lab.menus.writing.writing_prompts import (
    build_analysis_prompts, prompt_settings, save_prompt_settings,
)


APP = str(PACKAGE_ROOT / "menus/writing/writing_app.py")


@pytest.fixture
def questions():
    return [
        {"type_slot": 51, "topic": "물품 대여 문의", "context_title": "운동용품을 빌리고 싶습니다.",
         "passage": "이번 주말에 공을 (㉠). 대여 신청은 어디에서 (㉡)? 방법을 알려 주시면 감사하겠습니다."},
        {"type_slot": 52, "topic": "습도와 물의 증발",
         "passage": "공기가 건조하면 물이 (㉠). 그러나 습도가 높으면 (㉡). 이것은 공기 중 수증기의 양과 관계가 있다."},
        {"type_slot": 53, "topic": "지역 도서관 이용 변화", "data_source": "가상 조사 자료",
         "materials": [
             {"kind": "bar", "title": "이용자 수", "unit": "명", "categories": ["2020년", "2025년"],
              "series": [{"label": "이용자", "values": [200, 400]}]},
             {"kind": "table", "title": "이용 목적 순위", "columns": ["순위", "목적"],
              "rows": [["1위", "독서"], ["2위", "문화 활동"]]},
         ], "material_notes": ["원인: 운영 시간 확대"]},
        {"type_slot": 54, "topic": "공공시설 이용 예절",
         "passage": "공공시설은 많은 사람이 함께 이용한다. 아래 내용을 중심으로 공공시설 이용 예절에 대한 자신의 생각을 쓰라.",
         "requirements": ["이용 예절은 왜 중요한가?", "예절을 지키지 않으면 어떤 문제가 생기는가?", "이용 예절을 지키기 위해 어떤 노력이 필요한가?"]},
    ]


def result_for(questions):
    return manual_result("test", "test-model", "generation", json.dumps({"questions": questions}, ensure_ascii=False))


def test_all_28_references_keep_sources_and_original_instructions():
    refs = pipeline.load_references()
    assert {r.source_key for r in refs} == {f"{exam}:{slot}" for exam in (52, 60, 64, 83, 91, 96, 102) for slot in PROFILES}
    assert len(refs) == 28
    for ref in refs:
        assert ref.source_file == f"{ref.source_exam}_writing.pdf"
        assert (pipeline.PDF_ROOT / ref.source_file).is_file()
        assert ref.source_page == (1 if ref.question.type_slot < 53 else 2)
        assert ref.question.instruction == PROFILES[ref.question.type_slot].instruction.format(topic=ref.question.topic)
    old = next(r for r in refs if r.source_key == "52:51")
    assert "한 문장으로" in old.source_instruction
    assert "한 문장으로" not in old.question.instruction
    recent = next(r.question for r in refs if r.source_key == "102:53")
    assert recent.materials[0].series[0].values == [340, 650]
    assert recent.materials[0].unit == "만 명"
    ratios = next(r.question for r in refs if r.source_key == "96:53")
    assert ratios.materials[1].kind == "table"
    assert ratios.materials[1].rows == [["20~30대", "4배"], ["40~50대", "1.3배"]]


def test_prompts_use_only_selected_writing_references():
    system, user = pipeline.build_prompts([53], "지역 축제", pipeline.load_references())
    request = json.loads(user)
    assert request["생성할_문항번호_순서"] == [53]
    assert request["소재_요청"] == "지역 축제"
    assert len(request["참고_기출"]) == 7
    assert all(item["문항"]["type_slot"] == 53 for item in request["참고_기출"])
    assert "평가하거나 채점하지 않습니다" in system
    assert "가상 조사 자료" in system
    assert "600~700" not in system
    assert "200~300" in system
    with pytest.raises(ValueError):
        pipeline.build_prompts([50], "", [])


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_analysis_covers_all_exams_of_type_and_requires_multiple_exams(slot):
    references = pipeline.load_references()
    _, user = build_analysis_prompts(slot, references)
    request = json.loads(user)
    assert request["문항번호"] == slot
    assert len(request["참고_기출"]) == 7
    assert request["분석_범위"]["회차"] == [52, 60, 64, 83, 91, 96, 102]
    assert request["분석_범위"]["문항_수"] == 7
    assert request["새_문항_표준_발문"] == PROFILES[slot].instruction
    assert {item["문항"]["type_slot"] for item in request["참고_기출"]} == {slot}
    assert build_analysis_prompts(slot, list(reversed(references)))[1] == user
    with pytest.raises(ValueError, match="기출"):
        build_analysis_prompts(slot, [r for r in references if r.question.type_slot != slot])
    only_one_exam = [r for r in references if r.source_exam == 102]
    with pytest.raises(ValueError, match="두 회차"):
        build_analysis_prompts(slot, only_one_exam * 2)


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_common_analysis_ignores_old_exam_selection_and_reaches_generation(tmp_path, monkeypatch, slot):
    guide = "\n".join([
        "1. 유형의 목적\n해당 과제를 수행하는 쓰기 능력",
        "2. 공통 구조\n상황과 과제를 제시하고 수행 단서를 제공한다.",
        "3. 언어·논리적 특징\n문맥과 과제 사이의 논리적 연결",
        "4. 필수 조건\n표준 발문과 해당 유형의 형식·분량",
        "5. 변형 가능한 요소\n소재와 표현을 바꾸되 과제의 기능은 유지한다.",
        "6. 생성 체크리스트\n새 소재 선택 → 과제 설계 → 단서·논리·분량 점검",
    ])
    calls = []

    def analyze(*args, **kwargs):
        calls.append(args)
        return manual_result(args[0], args[1], "analysis", json.dumps({
            "type_slot": slot, "analysis_guide": guide,
        }, ensure_ascii=False))

    monkeypatch.setattr(pipeline, "call_provider", analyze)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.selectbox(key="writing-type").set_value(slot).run()
    app.selectbox(key=f"writing-reference-exam-{slot}").set_value(f"102:{slot}").run()
    app.session_state[f"writing-analysis-references-{slot}"] = [f"102:{slot}"]
    app.sidebar.radio(key="writing-stage").set_value("2. 유형 분석").run()
    assert not app.exception
    assert not any(widget.key == f"writing-analysis-references-{slot}" for widget in app.multiselect)
    assert any("전체 7회차 · 7문항" in text.value for text in app.caption)
    app.selectbox(key="writing-analysis-service").set_value("deepseek").run()
    app.button(key=f"writing-analyze-{slot}").click().run()
    assert not app.exception and not app.error
    assert len(calls) == 1 and calls[0][2] == "analysis"
    request = json.loads(calls[0][4])
    assert {ref["출처"] for ref in request["참고_기출"]} == {
        f"{exam}:{slot}" for exam in (52, 60, 64, 83, 91, 96, 102)
    }
    assert app.text_area(key=f"writing-analysis-guide-{slot}").value == guide
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    assert not app.exception
    generation_request = json.loads(next(t.value for t in app.text_area if t.label == "문항 생성 요청"))
    assert generation_request["유형_분석서"] == guide
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    assert json.loads(storage.list_runs("analysis")[0]["prompt_user"]) == request


def test_common_analysis_requires_cross_exam_data_before_api_or_import(monkeypatch):
    references = [r for r in pipeline.load_references() if r.source_exam == 102]
    monkeypatch.setattr(pipeline, "load_references", lambda: references)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(pipeline, "call_provider", lambda *a, **k: pytest.fail("incomplete reference set must not call API"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("2. 유형 분석").run()
    app.selectbox(key="writing-analysis-service").set_value("deepseek").run()
    app.text_area(key="writing-analysis-json-51").set_value(json.dumps({
        "type_slot": 51, "analysis_guide": "단일 회차 분석",
    })).run()
    assert not app.exception
    assert app.button(key="writing-analyze-51").disabled
    assert app.button(key="writing-analysis-import-51").disabled
    assert any("두 회차 이상의 기출" in text.value for text in app.info)


@pytest.mark.parametrize("payload", [
    {"type_slot": 52, "analysis_guide": "다른 유형 분석"},
    {"type_slot": 51, "analysis_guide": "  "},
    {"type_slot": 51},
    None,
])
def test_invalid_analysis_keeps_saved_guide_and_records_failure(tmp_path, payload):
    storage = WritingStorage(tmp_path / "writing.db")
    settings = {**prompt_settings(storage, 51), "analysis_guide": "기존 분석서"}
    save_prompt_settings(storage, 51, settings)
    result = manual_result("test", "test", "analysis", json.dumps(payload))
    outcome = pipeline.save_analysis_result(storage, result, "system", "user", 51)
    assert outcome["error"]
    assert prompt_settings(storage, 51) == settings
    assert storage.list_generated() == []
    run = storage.list_runs("analysis")[0]
    assert run["question_type"] == "writing_pbt_51"
    assert json.loads(run["result_json"])["error"]


def test_analysis_and_workshop_settings_reach_single_question_api(tmp_path, monkeypatch, questions):
    calls = []

    def call(*args, **kwargs):
        calls.append((args, kwargs))
        if args[2] == "analysis":
            return manual_result(args[0], args[1], "analysis", json.dumps({
                "type_slot": 51, "analysis_guide": "수신자와 발신자의 관계를 드러낸다.",
            }, ensure_ascii=False))
        return result_for(questions[:1])

    monkeypatch.setattr(pipeline, "call_provider", call)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("2. 유형 분석").run()
    app.selectbox(key="writing-analysis-service").set_value("deepseek").run()
    app.button(key="writing-analyze-51").click().run()
    assert not app.exception and not app.error
    assert app.text_area(key="writing-analysis-guide-51").value == "수신자와 발신자의 관계를 드러낸다."
    app.sidebar.radio(key="writing-stage").set_value("3. 프롬프트 작업실").run()
    app.text_area(key="writing-prompt-system-51").set_value("51번 전용 출제 지침")
    app.text_area(key="writing-prompt-guide-51").set_value("수신자·발신자 관계와 요청 목적")
    app.text_input(key="writing-prompt-difficulty-51").set_value("중급 초반")
    app.text_area(key="writing-prompt-instructions-51").set_value("문의와 요청 기능을 하나씩 배치")
    next(b for b in app.button if b.label == "프롬프트 설정 저장").click().run()
    assert not app.exception and not app.error
    app.selectbox(key="writing-type").set_value(52).run()
    assert app.text_area(key="writing-prompt-system-52").value != "51번 전용 출제 지침"
    app.selectbox(key="writing-type").set_value(51).run()
    assert app.text_area(key="writing-prompt-system-51").value == "51번 전용 출제 지침"
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    app.selectbox(key="writing-service").set_value("deepseek").run()
    app.text_area(key="writing-topic-51").set_value("신청 기간을 포함해 주세요.").run()
    system = next(t.value for t in app.text_area if t.label == "시스템 프롬프트")
    user = next(t.value for t in app.text_area if t.label == "문항 생성 요청")
    request = json.loads(user)
    assert request["생성할_문항번호_순서"] == [51]
    assert request["유형_분석서"] == "수신자·발신자 관계와 요청 목적"
    assert request["목표_난이도"] == "중급 초반"
    assert request["유형별_생성_지침"] == "문의와 요청 기능을 하나씩 배치"
    assert request["소재_요청"] == "신청 기간을 포함해 주세요."
    assert len(request["문항별_선택_소재"]) == 1
    app.button(key="writing-generate").click().run()
    assert not app.exception and not app.error
    assert len(calls) == 2 and [args[2] for args, _ in calls] == ["analysis", "generation"]
    assert calls[1][0][3:5] == (system, user)
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    assert len(storage.list_generated()) == 1
    assert storage.list_runs("generation")[0]["prompt_user"] == user
    assert prompt_settings(storage, 51)["system"] == system
    assert any(q.value == "51번 · 물품 대여 문의" for q in app.subheader)


def test_manual_analysis_is_saved_per_type_and_wrong_type_cannot_replace_it(tmp_path):
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("2. 유형 분석").run()
    for slot in (51, 52, 53, 54):
        app.selectbox(key="writing-type").set_value(slot).run()
        app.text_area(key=f"writing-analysis-json-{slot}").set_value(json.dumps({
            "type_slot": slot, "analysis_guide": f"{slot}번 분석서",
        }, ensure_ascii=False)).run()
        app.button(key=f"writing-analysis-import-{slot}").click().run()
        assert not app.exception and not app.error
    app.text_area(key="writing-analysis-json-54").set_value(json.dumps({
        "type_slot": 51, "analysis_guide": "잘못된 유형",
    })).run()
    app.button(key="writing-analysis-import-54").click().run()
    assert not app.exception and app.error
    assert app.text_area(key="writing-analysis-guide-54").value == "54번 분석서"
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    for slot in (51, 52, 53, 54):
        assert prompt_settings(storage, slot)["analysis_guide"] == f"{slot}번 분석서"


@pytest.mark.parametrize("source", ["api", "manual"])
def test_analysis_editor_updates_without_duplicate_default_warning(tmp_path, monkeypatch, source):
    from streamlit.elements.lib import policies

    # Streamlit emits this warning only once per process.
    monkeypatch.setattr(policies, "_shown_default_value_warning", False)
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    save_prompt_settings(storage, 51, {**prompt_settings(storage, 51), "analysis_guide": "저장된 분석서"})
    raw = json.dumps({"type_slot": 51, "analysis_guide": "새 분석 결과"}, ensure_ascii=False)
    monkeypatch.setattr(pipeline, "call_provider", lambda *a, **k: manual_result("deepseek", "test", "analysis", raw))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("2. 유형 분석").run()
    assert app.text_area(key="writing-analysis-guide-51").value == "저장된 분석서"
    assert not app.warning
    app.text_area(key="writing-analysis-guide-51").set_value("편집 중인 분석서").run()
    app.selectbox(key="writing-analysis-service").set_value("deepseek").run()
    assert app.text_area(key="writing-analysis-guide-51").value == "편집 중인 분석서"
    assert not app.warning
    if source == "api":
        app.button(key="writing-analyze-51").click().run()
    else:
        app.text_area(key="writing-analysis-json-51").set_value(raw).run()
        app.button(key="writing-analysis-import-51").click().run()
    assert not app.exception and not app.error
    assert not any("writing-analysis-guide-51" in warning.value for warning in app.warning)
    assert app.text_area(key="writing-analysis-guide-51").value == "새 분석 결과"
    app.text_area(key="writing-analysis-guide-51").set_value("검토한 분석서").run()
    app.button(key="writing-analysis-save-51").click().run()
    assert not app.exception and not app.error
    assert prompt_settings(storage, 51)["analysis_guide"] == "검토한 분석서"
    reopened = AppTest.from_file(APP, default_timeout=20).run()
    reopened.sidebar.radio(key="writing-stage").set_value("2. 유형 분석").run()
    assert reopened.text_area(key="writing-analysis-guide-51").value == "검토한 분석서"
    assert not reopened.warning


def test_generation_ui_rejects_multiple_questions_and_wrong_type(tmp_path, questions):
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    for invalid in (questions, questions[1:2]):
        app.text_area(key="writing-manual-json-51").set_value(json.dumps({"questions": invalid})).run()
        app.button(key="writing-import").click().run()
        assert not app.exception and app.error
    assert WritingStorage(tmp_path / "data/writing_pbt.db").list_generated() == []


@pytest.mark.parametrize("index,update", [
    (0, {"passage": "빈칸이 하나 (㉠)"}),
    (0, {"passage": "(㉠) (㉠) (㉡)"}),
    (0, {"choices": ["가", "나", "다", "라"]}),
    (0, {"score": 10}),
    (0, {"instruction": "알맞은 답을 고르십시오."}),
    (1, {"materials": [{"kind": "table", "title": "표", "columns": ["항목"], "rows": [["값"]]}]}),
    (2, {"materials": []}),
    (2, {"passage": "잘못된 지문"}),
    (3, {"requirements": ["한 개뿐인 요구 사항"]}),
    (3, {"requirements": ["같은 요구", "같은 요구", "같은 요구"]}),
    (3, {"passage": "  "}),
    (3, {"exam_format": "ibt"}),
])
def test_invalid_question_structures_are_rejected(questions, index, update):
    with pytest.raises(ValidationError):
        WritingQuestion.model_validate({**questions[index], **update})


@pytest.mark.parametrize("change", [
    {"series": [{"label": "이용자", "values": [10]}]},
    {"series": [{"label": "이용자", "values": [float('nan'), 20]}]},
    {"categories": ["동일 항목", "동일 항목"]},
    {"unit": ""},
    {"unit": "%", "series": [{"label": "비율", "values": [20, 110]}]},
    {"series": [{"label": "같은 이름", "values": [1, 2]}, {"label": "같은 이름", "values": [3, 4]}]},
])
def test_invalid_chart_data_is_rejected(questions, change):
    question = deepcopy(questions[2])
    question["materials"][0].update(change)
    with pytest.raises(ValidationError):
        WritingQuestion.model_validate(question)


@pytest.mark.parametrize("change", [{"rows": [["1위"]]}, {"rows": [["1위", ""]]}, {"columns": ["중복", "중복"]}])
def test_invalid_tables_are_rejected(questions, change):
    question = deepcopy(questions[2])
    question["materials"][1].update(change)
    with pytest.raises(ValidationError):
        WritingQuestion.model_validate(question)


def test_generation_calls_existing_client_once_and_saves_atomically(tmp_path, monkeypatch, questions):
    calls = []

    def generate(*args, **kwargs):
        calls.append((args, kwargs))
        return result_for(questions)

    monkeypatch.setattr(pipeline, "call_provider", generate)
    store = WritingStorage(tmp_path / "writing.db")
    topics = TopicStore(tmp_path / "topics.db")
    plan = pipeline.assign_writing_topics(topics, [51, 52, 53, 54])
    system, user = pipeline.build_prompts([51, 52, 53, 54], "", [], plan)
    outcome = pipeline.generate_writing(store, provider="test", model="test-model", system=system, user=user,
                                        slots=[51, 52, 53, 54], topic_plan=plan, topic_store=topics)
    assert not outcome["error"]
    assert len(calls) == 1 and calls[0][0][2] == "generation"
    assert calls[0][0][3:] == (system, user)
    assert len(topics.list_slot_pool_usages()) == 4
    restored = WritingStorage(store.path)
    rows = sorted(restored.list_generated(), key=lambda row: row["position"])
    assert [row["question"]["type_slot"] for row in rows] == [51, 52, 53, 54]
    assert all(row["run_id"] == outcome["run_id"] for row in rows)
    assert restored.list_runs()[0]["prompt_system"] == system
    with restored.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["bad_structure", "wrong_count", "wrong_order", "provider", "invalid_json", "duplicate"])
def test_failures_record_run_without_partial_questions(tmp_path, questions, failure):
    store = WritingStorage(tmp_path / "writing.db")
    slots = [51, 52, 53, 54]
    if failure == "bad_structure":
        questions[3]["requirements"] = []
    if failure == "wrong_count":
        questions.pop()
    if failure == "wrong_order":
        questions.reverse()
    if failure == "duplicate":
        questions = [questions[0], questions[0]]
        slots = [51, 51]
    result = result_for(questions)
    if failure == "provider":
        result = ProviderResult(provider="test", model="test", operation="generation", error="서비스 연결 실패")
    if failure == "invalid_json":
        result = manual_result("test", "test", "generation", "not json")
    outcome = pipeline.save_result(store, result, "system", "user", slots)
    assert outcome["error"] and not outcome["question_ids"]
    assert store.list_generated() == []
    assert len(store.list_runs()) == 1
    assert json.loads(store.list_runs()[0]["result_json"])["error"]


def test_sql_failure_rolls_back_run_and_questions(tmp_path, questions):
    store = WritingStorage(tmp_path / "writing.db")
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON generated_questions WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        pipeline.save_result(store, result_for(questions), "system", "user", [51, 52, 53, 54])
    assert store.list_generated() == []
    assert store.list_runs() == []


def test_edits_keep_originals_and_exports_use_saved_revision(tmp_path, questions):
    store = WritingStorage(tmp_path / "writing.db")
    outcome = pipeline.save_result(store, result_for(questions), "system", "user", [51, 52, 53, 54])
    target = outcome["question_ids"][2]
    edited = deepcopy(questions[2])
    edited["topic"] = "수정한 도서관 이용 변화"
    edited["materials"][0]["series"][0]["values"] = [300, 600]
    store.save_generated_edit(target, edited)
    row = next(row for row in store.list_generated() if row["id"] == target)
    assert json.loads(row["data_json"])["topic"] == questions[2]["topic"]
    question = WritingQuestion.model_validate(row["question"])
    assert "수정한" in question.instruction
    exported = pipeline.export_json([question])
    assert WritingPayload.model_validate_json(exported).questions == [question]
    text = pipeline.export_text([question])
    assert "2020년 | 300" in text and "2025년 | 600" in text
    assert "1위 | 독서" in text and "200~300" in text
    with pytest.raises(ValueError):
        store.save_generated_edit(target, questions[0])
    assert next(row for row in store.list_generated() if row["id"] == target)["question"] == question.model_dump()


def test_all_reference_types_and_generation_options_render():
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert not app.exception
    for slot in (51, 52, 53, 54):
        app.selectbox(key="writing-type").set_value(slot).run()
        assert not app.exception
    app.checkbox(key="writing-show-pdf").check().run()
    assert not app.exception
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    for scope in (51, 52, 53, 54):
        app.selectbox(key="writing-type").set_value(scope).run()
        assert not app.exception
    for service in ("deepseek", "bizrouter", "chatkhu"):
        app.selectbox(key="writing-service").set_value(service).run()
        assert not app.exception
        assert app.button(key="writing-generate").disabled


def test_individual_import_edit_and_reload_through_ui(tmp_path, questions):
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    for question in questions:
        slot = question["type_slot"]
        app.selectbox(key="writing-type").set_value(slot).run()
        app.text_area(key=f"writing-manual-json-{slot}").set_value(json.dumps({"questions": [question]}, ensure_ascii=False)).run()
        app.button(key="writing-import").click().run()
        assert not app.exception and not app.error
        assert any("1문항 저장 완료" in item.value for item in app.success)
    app.sidebar.radio(key="writing-stage").set_value("5. 수정·내보내기").run()
    for slot in (51, 52, 53, 54):
        app.selectbox(key="writing-filter").set_value(slot).run()
        assert not app.exception
        app.button(key="writing-save").click().run()
        assert not app.exception and not app.error
    app.selectbox(key="writing-filter").set_value(51).run()
    passage = next(widget for widget in app.text_area if widget.label == "지문")
    passage.set_value("내일 공을 (㉠). 신청서를 어디에 (㉡)?").run()
    app.button(key="writing-save").click().run()
    assert not app.exception and not app.error
    store = WritingStorage(tmp_path / "data/writing_pbt.db")
    edited = next(row for row in store.list_generated() if row["question"]["type_slot"] == 51)
    assert edited["question"]["passage"].startswith("내일 공을")
    assert len(store.list_generated()) == 4
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("5. 수정·내보내기").run()
    assert not app.exception
    assert len(app.selectbox(key="writing-selected-None").options) == 4


def topic_brief(store, item, position=1, slot=51):
    pool = store.get_slot_pool(WRITING_POOL_ID)
    return TopicBrief(unit_key=f"item-{position}", slot_numbers=[slot],
                      topic_id=slot_item_topic_id(pool.id, item), domain=pool.name,
                      title=item, angle=pool.prompt_instruction, source="slot_pool",
                      pool_id=pool.id, prompt_instruction=pool.prompt_instruction)


def test_writing_topic_seed_retains_both_levels_and_user_edits(tmp_path):
    store = TopicStore(tmp_path / "topics.db")
    pool = store.get_slot_pool(WRITING_POOL_ID)
    grouped = {level: [split_writing_topic(item) for item in pool.items if item.startswith(level)]
               for level in ("중급", "고급")}
    assert len(grouped["중급"]) == 43 and len(grouped["고급"]) == 42
    assert len({item[1] for item in grouped["중급"]}) == 11
    assert len({item[1] for item in grouped["고급"]}) == 13
    assert "중급 > 교육 > 유학" in pool.items and "고급 > 교육 > 유학" in pool.items
    assert "고급 > 전문 분야 > 성" in pool.items
    assert "고급 > 전문 분야 > 철학/윤리" in pool.items
    assert "중급 > 생활 방식 > 성격/감정" in pool.items
    for section, slots in (("reading", [51]), ("listening", [54]), ("writing", [50]), ("writing", [55])):
        with pytest.raises(ValidationError):
            SlotTopicPool.model_validate({**pool.model_dump(), "section": section, "slot_numbers": slots})
    store.save_slot_pool(pool.model_copy(update={"items": ["중급 > 교육 > 사용자 소재"]}))
    reloaded = TopicStore(store.path)
    assert reloaded.get_slot_pool(pool.id).items == ["중급 > 교육 > 사용자 소재"]
    reloaded.set_slot_pool_active(pool.id, False)
    assert reloaded.resolve_source("writing", "writing_pbt", [51]).source == "none"


def test_selected_topics_bind_to_positions_even_for_repeated_slots(tmp_path):
    store = TopicStore(tmp_path / "topics.db")
    plan = [topic_brief(store, "중급 > 생활 방식 > 전화"),
            topic_brief(store, "중급 > 교육 > 유학", position=2)]
    _, user = pipeline.build_prompts([51, 51], "행사 안내", [], plan)
    request = json.loads(user)
    assert [(row["생성_순서"], row["문항번호"], row["수준"], row["분야"], row["소재"])
            for row in request["문항별_선택_소재"]] == [(1, 51, "중급", "생활 방식", "전화"), (2, 51, "중급", "교육", "유학")]
    assert request["소재_요청"] == "행사 안내"
    with pytest.raises(ValueError):
        pipeline.build_prompts([51, 52], "", [], plan)


@pytest.mark.parametrize("valid", [True, False])
def test_selected_topic_snapshot_and_usage_only_after_success(tmp_path, questions, valid):
    topics = TopicStore(tmp_path / "topics.db")
    writing = WritingStorage(tmp_path / "writing.db")
    plan = [topic_brief(topics, "중급 > 건강·운동·쇼핑 > 운동")]
    system, user = pipeline.build_prompts([51], "", [], plan)
    result = result_for(questions[:1] if valid else questions[1:2])
    outcome = pipeline.save_result(writing, result, system, user, [51], topic_plan=plan, topic_store=topics)
    snapshot = json.loads(writing.list_runs()[0]["result_json"])["request_parameters"]["writing_topic_plan"]
    assert snapshot[0]["title"] == plan[0].title
    assert len(topics.list_slot_pool_usages()) == int(valid)
    assert bool(outcome["question_ids"]) == valid
    if valid:
        usage = topics.list_slot_pool_usages()[0]
        assert usage["run_id"] == outcome["run_id"] and usage["section"] == "writing"
    assert topics.list_usages() == []


def test_inactive_selection_blocks_api_call(tmp_path, monkeypatch):
    topics = TopicStore(tmp_path / "topics.db")
    plan = [topic_brief(topics, "중급 > 교육 > 유학")]
    topics.set_slot_pool_active(WRITING_POOL_ID, False)
    monkeypatch.setattr(pipeline, "call_provider", lambda *a, **k: pytest.fail("inactive topic must not call provider"))
    outcome = pipeline.generate_writing(WritingStorage(tmp_path / "writing.db"), provider="test", model="test",
                                        system="s", user="u", slots=[51], topic_plan=plan, topic_store=topics)
    assert "비활성화" in outcome["error"]
    assert topics.list_slot_pool_usages() == []


def test_random_topics_stay_stable_until_reroll_or_success_and_use_live_bank(tmp_path, questions, monkeypatch):
    from topik_question_lab.menus.writing import writing_topic_ui

    calls = []

    def assign(store, slots):
        calls.append(list(slots))
        return pipeline.assign_writing_topics(store, slots, rng=random.Random(len(calls)))

    monkeypatch.setattr(writing_topic_ui, "assign_writing_topics", assign)
    topics = TopicStore(tmp_path / "data/topic_bank.db")
    pool = topics.get_slot_pool(WRITING_POOL_ID)
    items = ["중급 > 교육 > 유학", "중급 > 교육 > 학교생활", "고급 > 과학 > 새 기술 소재", "고급 > 과학 > 미래 사회"]
    topics.save_slot_pool(pool.model_copy(update={"items": items}))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    app.selectbox(key="writing-type").set_value(53).run()
    assert not app.exception

    def selected():
        return json.loads(next(area.value for area in app.text_area if area.label == "문항 생성 요청"))["문항별_선택_소재"]

    plan = selected()
    count = len(calls)
    assert [row["수준"] for row in plan] == ["고급"]
    assert [row["문항번호"] for row in plan] == [53]
    assert len({row["소재_ID"] for row in plan}) == 1
    app.text_area(key="writing-topic-53").set_value("학교 행사와 연결해 주세요.").run()
    app.selectbox(key="writing-service").set_value("deepseek").run()
    assert selected() == plan and len(calls) == count
    app.button(key="writing-reroll-topics").click().run()
    assert len(calls) == count + 1
    plan = selected()
    app.text_area(key="writing-manual-json-53").set_value("invalid json").run()
    app.button(key="writing-import").click().run()
    assert app.error and selected() == plan
    app.run()
    assert len(calls) == count + 1
    app.text_area(key="writing-manual-json-53").set_value(json.dumps({"questions": questions[2:3]}, ensure_ascii=False)).run()
    app.button(key="writing-import").click().run()
    assert not app.exception and not app.error
    assert {row["item"] for row in topics.list_slot_pool_usages()} == {f"{plan[0]['수준']} > {plan[0]['분야']} > {plan[0]['소재']}"}
    writing = WritingStorage(tmp_path / "data/writing_pbt.db")
    saved_request = json.loads(writing.list_runs()[0]["prompt_user"])
    assert saved_request["문항별_선택_소재"] == plan
    app.run()
    assert not app.exception and len(calls) == count + 2
    assert app.text_area(key="writing-manual-json-53").value == ""
    topics.save_slot_pool(pool.model_copy(update={"items": items[:2]}))
    app.run()
    assert not app.exception
    assert any("53번에 배정할 고급 소재가 없습니다" in message.value for message in app.error)
    assert not any(button.key in {"writing-generate", "writing-import"} for button in app.button)


@pytest.mark.parametrize("slots", [[51, 52, 53, 54], [51] * 10, [53] * 10, [54, 52, 53, 51]])
def test_random_assignment_obeys_slot_level_and_avoids_duplicates(tmp_path, slots):
    topics = TopicStore(tmp_path / "topics.db")
    plan = pipeline.assign_writing_topics(topics, slots, rng=random.Random(7))
    assert [brief.slot_numbers[0] for brief in plan] == slots
    assert [split_writing_topic(brief.title)[0] for brief in plan] == ["중급" if slot < 53 else "고급" for slot in slots]
    assert len({brief.title for brief in plan}) == len(slots)
    another = pipeline.assign_writing_topics(topics, slots, rng=random.Random(12))
    assert [b.title for b in plan] != [b.title for b in another]
    assert topics.list_slot_pool_usages() == []
    pipeline.validate_topic_plan(slots, plan, topics)


def test_assignment_prefers_less_used_topics_and_handles_small_pools(tmp_path):
    topics = TopicStore(tmp_path / "topics.db")
    pool = topics.get_slot_pool(WRITING_POOL_ID)
    topics.save_slot_pool(pool.model_copy(update={"items": ["중급 > 교육 > 유학", "중급 > 교육 > 학교생활"]}))
    used = topic_brief(topics, "중급 > 교육 > 유학")
    topics.record_usages([used], section="writing", question_type="writing_pbt", provider="test", model="test", run_id=1)
    plan = pipeline.assign_writing_topics(topics, [51] * 3, rng=random.Random(1))
    assert [b.title for b in plan[:2]] == ["중급 > 교육 > 학교생활", "중급 > 교육 > 유학"]
    assert all(b.title.startswith("중급 >") for b in plan)
    with pytest.raises(ValueError, match="고급 소재가 없습니다"):
        pipeline.assign_writing_topics(topics, [53])


@pytest.mark.parametrize("case", ["wrong_level", "missing"])
def test_wrong_level_or_missing_assignment_blocks_api(tmp_path, monkeypatch, case):
    topics = TopicStore(tmp_path / "topics.db")
    plan = [topic_brief(topics, "고급 > 과학 > 기술")] if case == "wrong_level" else []
    monkeypatch.setattr(pipeline, "call_provider", lambda *a, **k: pytest.fail("invalid plan must not call provider"))
    outcome = pipeline.generate_writing(WritingStorage(tmp_path / "writing.db"), provider="test", model="test",
                                        system="s", user="u", slots=[51], topic_plan=plan, topic_store=topics)
    assert outcome["error"] and not outcome["question_ids"]


def test_default_guidance_refresh_keeps_user_edits_and_topics(tmp_path):
    store = TopicStore(tmp_path / "topics.db")
    pool = store.get_slot_pool(WRITING_POOL_ID)
    legacy = pool.prompt_instruction.replace("51·52번은 중급, 53·54번은 고급 소재를 사용하십시오.",
                                             "분류는 문항 번호를 제한하지 않습니다.")
    with store.connect() as db:
        db.execute("UPDATE slot_topic_pools SET prompt_instruction=? WHERE id=?", (legacy, pool.id))
    restored = TopicStore(store.path)
    assert restored.get_slot_pool(pool.id).prompt_instruction == pool.prompt_instruction
    assert restored.get_slot_pool(pool.id).items == pool.items
    restored.save_slot_pool(pool.model_copy(update={"prompt_instruction": "사용자 지침"}))
    assert TopicStore(store.path).get_slot_pool(pool.id).prompt_instruction == "사용자 지침"
