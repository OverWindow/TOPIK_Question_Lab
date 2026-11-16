import json
from copy import deepcopy

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.generation.providers import manual_result
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.writing_evaluation import (
    OPERATION, SCORE_LIMITS, EvaluationRequest, LearnerAnswer, build_evaluation_request, character_counts, digest,
    comparison_rows, evaluate_writing, human_review, save_evaluation, save_human_review, validate_evaluation,
)
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing.writing_evaluation_prompts import (
    DEFAULT_SYSTEMS, evaluation_settings, load_answer_references, rubric_for, save_evaluation_settings,
)
from topik_question_lab.menus.writing.writing_pipeline import PDF_ROOT, load_references


APP = str(PACKAGE_ROOT / "menus/writing/writing_evaluation_app.py")


def make_request(source="102:51", *, answer=None, system=None, notes=""):
    refs = load_references()
    ref = next(r for r in refs if r.source_key == source)
    answer_ref = load_answer_references(refs)[source]
    if answer is None:
        answer = (LearnerAnswer(a=answer_ref.blank_answers["a"][0], b=answer_ref.blank_answers["b"][0])
                  if ref.question.type_slot < 53 else LearnerAnswer(text=answer_ref.text))
    return build_evaluation_request(ref, answer,
                                    system=DEFAULT_SYSTEMS[ref.question.type_slot] if system is None else system,
                                    rubric=rubric_for(ref.question.type_slot), notes=notes)


def response_data(request):
    """Transport fixture scores are not human calibration labels."""
    payload = request.payload
    slot, original = payload["type_slot"], payload["answer"]
    quote = next((value for value in original.values() if value.strip()), "")
    weights = payload.get("reference_answer", {}).get("blank_weights", {})
    return {
        "request_id": request.request_id, "type_slot": slot,
        "scores": {key: {"score": maximum, "rationale": "모의 모델 응답", "quotes": [original[key] if slot < 53 else quote],
                         "parts": weights.get(key, {})} for key, maximum in SCORE_LIMITS[slot].items()},
        "task_checks": [{"requirement_id": key, "status": "충족", "reason": "모의 수행 확인",
                         "quotes": [original[key] if slot < 53 else quote]}
                        for key in payload["tasks"]],
        "summary": "전송·검증 테스트용 응답", "corrections": [], "review_reasons": [],
    }


def result_for(data):
    return manual_result("test", "test-model", OPERATION, json.dumps(data, ensure_ascii=False))


def test_source_answers_link_all_questions_without_inventing_human_scores():
    refs = load_references()
    answers = load_answer_references(refs)
    assert len(answers) == 28
    for key, answer in answers.items():
        assert (PDF_ROOT / answer.source_file).is_file()
        assert "score" not in answer.model_dump()
        assert answer.source_page == (2 if key in ("60:53", "60:54") else 1)
    assert answers["102:51"].blank_answers["b"] == ["시작되기 전에"]
    assert answers["96:53"].source_char_count == 280
    assert "2.5배" in answers["96:53"].text
    assert "24조 2천억" in answers["91:53"].text
    assert answers["60:51"].blank_weights["a"] == {"content": 3, "form": 2}
    assert answers["60:51"].blank_weights["b"] == {"content": 2, "form": 3}
    assert answers["60:52"].blank_weights["a"] == {"content": 2, "form": 3}
    assert not answers["102:51"].blank_weights
    assert all(not a.published_rules for key, a in answers.items() if not key.startswith("60:"))


@pytest.mark.parametrize("source", [f"{exam}:{slot}" for exam in (52, 60, 64, 83, 91, 96, 102) for slot in (51, 52, 53, 54)])
def test_every_source_builds_and_validates_transport_response(source):
    request = make_request(source)
    assert "reference_answer" not in request.payload
    evaluation = validate_evaluation(result_for(response_data(request)), request)
    assert evaluation.total == {51: 10, 52: 10, 53: 30, 54: 50}[request.payload["type_slot"]]


def test_request_preserves_original_context_and_binds_source_answer_and_prompt():
    answer = LearnerAnswer(a=" 정리하면 됩니다 ", b="시작되기 전에")
    request = make_request(answer=answer)
    assert request.payload["answer"]["a"] == " 정리하면 됩니다 "
    assert "방학이 시작되기 전에 개인 물건" in request.payload["completed_passage"]
    assert request.request_id == make_request(answer=answer).request_id
    assert request.request_id != make_request(answer=answer, notes="추가 실험 지침").request_id
    assert request.request_id != make_request(answer=answer, system=DEFAULT_SYSTEMS[51] + "\n추가").request_id
    assert "한 문장" not in request.payload["question"]["source_instruction"]
    assert "동일한 결함" in request.system and "실험적 추정" in request.system
    assert not any("human" in key for key in request.payload)
    special = make_request(answer=LearnerAnswer(a="(㉡)", b="테스트"))
    assert "먼저 (㉡)." in special.payload["completed_passage"]


def test_rubrics_and_materials_keep_source_values():
    p = make_request("102:53").payload
    assert p["question"]["question"]["materials"][0]["series"][0]["values"] == [340, 650]
    assert p["question"]["question"]["materials"][0]["unit"] == "만 명"
    assert len(p["tasks"]) == 5
    assert rubric_for(53)["published_bands"]["language_raw"]["C"] == [5, 6]
    assert rubric_for(54)["published_bands"]["content_and_organization"]["A"] == [11, 12]
    assert make_request("60:54").payload["tasks"]["requirement:3"].startswith("조기 교육에 찬성")


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(request_id="0" * 64),
    lambda d: d.update(type_slot=52),
    lambda d: d["scores"]["a"].update(score=6),
    lambda d: d["scores"]["a"].update(score=True),
    lambda d: d["scores"]["a"].update(score="5"),
    lambda d: d["scores"].pop("b"),
    lambda d: d["scores"]["a"].update(quotes=["원본에 없는 말"]),
    lambda d: d["scores"]["a"].update(quotes=["시작되기 전에"]),
    lambda d: d["scores"]["a"].update(quotes=[]),
    lambda d: d["scores"]["a"].update(parts={"content": 3, "form": 2}),
    lambda d: d["task_checks"].pop(),
    lambda d: d["task_checks"].append(deepcopy(d["task_checks"][0])),
    lambda d: d["task_checks"][0].update(quotes=["가짜 인용"]),
    lambda d: d["task_checks"][1].update(quotes=["정리해야 합니다"]),
    lambda d: d.update(corrections=[{"original": "가짜 오류", "suggestion": "수정", "reason": "모의"}]),
    lambda d: d.update(total_score=10),
])
def test_rejects_unbound_or_invalid_evaluation(mutation):
    request = make_request()
    data = response_data(request)
    mutation(data)
    with pytest.raises((ValidationError, ValueError)):
        validate_evaluation(result_for(data), request)


def test_specific_parts_essay_language_and_unanswered_blank():
    request = make_request("60:51")
    data = response_data(request)
    data["scores"]["a"]["parts"] = {"content": 2, "form": 2}
    with pytest.raises(ValueError, match="세부 배점"):
        validate_evaluation(result_for(data), request)
    request = make_request("102:53")
    data = response_data(request)
    data["scores"]["language"]["score"] = 15
    with pytest.raises(ValueError, match="짝수"):
        validate_evaluation(result_for(data), request)
    request = make_request(answer=LearnerAnswer(a="정리해야 합니다"))
    with pytest.raises(ValueError, match="미응답"):
        validate_evaluation(result_for(response_data(request)), request)


@pytest.mark.parametrize("source", ["60:51", "60:52", "102:53", "102:54"])
def test_empty_answer_never_calls_model(source, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("An empty answer must not cause a paid model call")
    monkeypatch.setattr("topik_question_lab.common.writing_evaluation.call_provider", unexpected)
    request = make_request(source, answer=LearnerAnswer())
    result = evaluate_writing(request, provider="test", model="test")
    assert validate_evaluation(result, request).total == 0
    assert result.provider == "local"


@pytest.mark.parametrize("source", ["60:51", "60:52", "102:53", "102:54"])
def test_old_reference_based_results_remain_readable_but_cannot_be_called(source, tmp_path, monkeypatch):
    current = make_request(source)
    payload = current.payload
    payload.pop("request_id")
    payload["reference_answer"] = load_answer_references(load_references())[source].model_dump()
    old_id = digest({"system": current.system, "payload": payload})
    payload["request_id"] = old_id
    old = EvaluationRequest(current.system, json.dumps(payload, ensure_ascii=False), old_id)
    data = response_data(old)
    result = result_for(data)
    assert validate_evaluation(result, old).total == sum(SCORE_LIMITS[payload["type_slot"]].values())
    storage = WritingStorage(tmp_path / "legacy.db")
    outcome = save_evaluation(storage, result, old)
    assert not outcome["error"]
    save_human_review(storage, outcome["run_id"], SCORE_LIMITS[payload["type_slot"]], "기존 기록 조회 확인")
    assert comparison_rows(storage, storage.list_runs(OPERATION))[0]["차이(AI−사람)"] == 0
    if payload["type_slot"] < 53:
        data["scores"]["a"]["parts"] = {"content": 0, "form": 0}
        with pytest.raises(ValueError, match="합계"):
            validate_evaluation(result_for(data), old)

    def unexpected(*args, **kwargs):
        pytest.fail("A legacy request must not be sent to a model")
    monkeypatch.setattr("topik_question_lab.common.writing_evaluation.call_provider", unexpected)
    with pytest.raises(ValueError, match="기존 요청"):
        evaluate_writing(old, provider="test", model="test")


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_request_building_does_not_load_or_require_answer_reference(slot, monkeypatch):
    from topik_question_lab.menus.writing import writing_evaluation_prompts as prompts
    ref = next(r for r in load_references() if r.source_key == f"102:{slot}")
    def unexpected(*args, **kwargs):
        pytest.fail("Request construction must not load a reference answer")
    monkeypatch.setattr(prompts, "load_answer_references", unexpected)
    answer = LearnerAnswer(a="내 답안", b="다른 답안") if slot < 53 else LearnerAnswer(text="독립적으로 작성한 답안이다.")
    request = build_evaluation_request(ref, answer, system=DEFAULT_SYSTEMS[slot], rubric=rubric_for(slot))
    assert "reference_answer" not in request.payload
    assert request.payload["answer"] == answer.for_slot(slot)


def test_legacy_saved_prompt_is_preserved_without_overriding_reference_free_default(tmp_path):
    storage = WritingStorage(tmp_path / "settings.db")
    legacy = '{"system":"reference_answer를 사용하라", "notes":"기존 지침"}'
    storage.set_setting("writing:evaluation:prompt:51", legacy)
    assert evaluation_settings(storage, 51) == {"system": DEFAULT_SYSTEMS[51], "notes": ""}
    save_evaluation_settings(storage, 51, "새 평가 지침", "새 지침")
    assert evaluation_settings(storage, 51)["system"] == "새 평가 지침"
    assert storage.get_setting("writing:evaluation:prompt:51") == legacy


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_backend_question_uses_actual_identity_without_reference_or_pdf_metadata(slot):
    ref = next(r for r in load_references() if r.source_key == f"102:{slot}")
    answer = LearnerAnswer(a="작성한 답안", b="다른 답안") if slot < 53 else LearnerAnswer(text="작성한 글이다.")
    options = {"system": DEFAULT_SYSTEMS[slot], "rubric": rubric_for(slot)}
    request = build_evaluation_request(ref.question, answer, source_key=f"writing-set:fixture:{slot}", **options)
    assert request.payload["source_key"] == f"writing-set:fixture:{slot}"
    assert request.payload["question"] == {"question": ref.question.model_dump(), "source_instruction": ref.question.instruction}
    assert "reference_answer" not in request.payload
    assert validate_evaluation(result_for(response_data(request)), request).total == sum(SCORE_LIMITS[slot].values())
    other = build_evaluation_request(ref.question, answer, source_key=f"writing-set:other:{slot}", **options)
    assert other.request_id != request.request_id
    with pytest.raises(ValueError, match="식별자"):
        build_evaluation_request(ref.question, answer, **options)


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_model_call_uses_evaluation_operation_and_saved_request(monkeypatch, slot):
    request = make_request(f"102:{slot}")
    calls = []
    def call(*args, **kwargs):
        calls.append((args, kwargs))
        return result_for(response_data(request))
    monkeypatch.setattr("topik_question_lab.common.writing_evaluation.call_provider", call)
    evaluate_writing(request, provider="test", model="test", model_capabilities={"flag": True})
    assert request.system == DEFAULT_SYSTEMS[slot]
    assert calls[0][0] == ("test", "test", OPERATION, request.system, request.user)
    assert calls[0][1] == {"model_capabilities": {"flag": True}}
    assert "reference_answer" not in json.loads(calls[0][0][4])


def test_character_count_is_explicit_and_wrong_answer_fields_are_rejected():
    counts = character_counts(" 가\r\n나 😀 ")
    assert counts["공백_포함_줄바꿈_제외"] == 6
    assert counts["모든_공백_제외"] == 3
    with pytest.raises(ValueError):
        make_request(answer=LearnerAnswer(text="잘못된 필드"))


def test_storage_snapshots_failures_human_comparison_and_prompt_isolation(tmp_path):
    storage = WritingStorage(tmp_path / "test.db")
    with storage.connect() as conn:
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    storage.set_setting("writing:51:prompt", '{"system":"generation-only"}')
    save_evaluation_settings(storage, 51, DEFAULT_SYSTEMS[51], "형식 검토")
    assert evaluation_settings(storage, 51)["notes"] == "형식 검토"
    assert evaluation_settings(storage, 52)["notes"] == ""
    assert json.loads(storage.get_setting("writing:51:prompt"))["system"] == "generation-only"
    request = make_request()
    outcome = save_evaluation(storage, result_for(response_data(request)), request)
    assert not outcome["error"]
    run = storage.list_runs(OPERATION)[0]
    assert run["prompt_user"] == request.user and run["prompt_system"] == request.system
    assert not storage.list_generated()
    save_human_review(storage, outcome["run_id"], {"a": 4, "b": 5}, "문맥은 충족하나 표현 검토")
    assert human_review(storage, outcome["run_id"])["scores"] == {"a": 4, "b": 5}
    assert comparison_rows(storage, storage.list_runs(OPERATION))[0]["차이(AI−사람)"] == 1
    bad = response_data(request)
    bad["request_id"] = "0" * 64
    failed = save_evaluation(storage, result_for(bad), request)
    assert failed["error"]
    assert len(comparison_rows(storage, storage.list_runs(OPERATION))) == 1
    with pytest.raises(ValueError):
        save_human_review(storage, failed["run_id"], {"a": 4, "b": 5}, "잘못된 실행")
    with pytest.raises(ValueError):
        save_human_review(storage, outcome["run_id"], {"a": 10, "b": 5}, "범위 초과")
    with pytest.raises(ValueError):
        save_human_review(storage, outcome["run_id"], {"a": 4, "b": 5}, " ")
    with storage.connect() as conn:
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert after == before


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_new_default_changes_new_requests_without_rewriting_saved_results(tmp_path, monkeypatch, slot):
    from topik_question_lab.menus.writing import writing_evaluation_prompts as prompts

    storage = WritingStorage(tmp_path / "test.db")
    monkeypatch.setattr(prompts, "DEFAULT_SYSTEMS", {**DEFAULT_SYSTEMS, slot: "이전에 사용한 평가 프롬프트"})
    old_request = make_request(f"60:{slot}", system=evaluation_settings(storage, slot)["system"])
    old_result = result_for(response_data(old_request))
    old_outcome = save_evaluation(storage, old_result, old_request)
    old_run = storage.list_runs(OPERATION)[0]
    save_human_review(storage, old_outcome["run_id"], SCORE_LIMITS[slot], "이전 평가의 사람 채점")
    old_review = human_review(storage, old_outcome["run_id"])

    monkeypatch.setattr(prompts, "DEFAULT_SYSTEMS", DEFAULT_SYSTEMS)
    new_request = make_request(f"60:{slot}", system=evaluation_settings(storage, slot)["system"])
    assert "경계 사례의 보수적 판단" in new_request.system
    assert old_request.request_id != new_request.request_id
    for key in ("answer", "rubric", "score_limits", "output_schema"):
        assert new_request.payload[key] == old_request.payload[key]
    # Prompt policy must not become a score cap or an automatic deduction.
    new_result = result_for(response_data(new_request))
    assert validate_evaluation(new_result, new_request).total == sum(SCORE_LIMITS[slot].values())
    assert validate_evaluation(old_result, old_request).total == sum(SCORE_LIMITS[slot].values())
    new_outcome = save_evaluation(storage, new_result, new_request)
    runs = {run["id"]: run for run in storage.list_runs(OPERATION)}
    assert runs[old_outcome["run_id"]] == old_run
    assert runs[new_outcome["run_id"]]["prompt_system"] == DEFAULT_SYSTEMS[slot]
    assert human_review(storage, old_outcome["run_id"]) == old_review
    assert json.loads(runs[new_outcome["run_id"]]["result_json"])["request_parameters"]["prompt_version"] != (
        json.loads(old_run["result_json"])["request_parameters"]["prompt_version"])


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_new_default_respects_explicit_saved_evaluation_prompt(tmp_path, slot):
    storage = WritingStorage(tmp_path / "test.db")
    save_evaluation_settings(storage, slot, "사용자가 저장한 평가 지침", "저장된 보조 지침")
    assert evaluation_settings(storage, slot) == {"system": "사용자가 저장한 평가 지침", "notes": "저장된 보조 지침"}
    for other in set(DEFAULT_SYSTEMS) - {slot}:
        assert evaluation_settings(storage, other) == {"system": DEFAULT_SYSTEMS[other], "notes": ""}


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_type_prompts_keep_only_relevant_rules_and_common_scoring_guards(slot):
    system = make_request(f"102:{slot}").system
    assert f"{slot}번" in system
    assert all(f"{other}번" not in system for other in set(DEFAULT_SYSTEMS) - {slot})
    for rule in ("경계 사례의 보수적 판단", "상위 점수를 뒷받침하는 근거", "높은 점수와 만점을 허용",
                 "동일 결함의 중복 감점", "원본에서 그대로 가져온 quotes", "출처 기준을 적용"):
        assert rule in system
    if slot < 53:
        assert "completed_passage" in system and "parts는 빈 객체" in system
        assert "language_multiplier" not in system and "published_bands" not in system
    else:
        assert "language_multiplier" in system and "published_bands" in system
        assert "completed_passage" not in system and "blank_weights" not in system
    assert "reference_answer" not in system and "참고답안은 제공되지 않는다" in system


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_workshop_loads_all_types_and_sample_answers(slot):
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.selectbox(key="writing-evaluation-type").set_value(slot).run()
    assert not app.exception
    assert app.text_area(key=f"evaluation-system-{slot}").value == DEFAULT_SYSTEMS[slot]
    app.button(key=f"evaluation-load-answer-102:{slot}").click().run()
    key = "a" if slot < 53 else "text"
    assert app.text_area(key=f"evaluation-answer-102:{slot}-{key}").value
    assert not app.exception


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_workshop_refreshes_changed_default_and_keeps_saved_prompt(tmp_path, monkeypatch, slot):
    from topik_question_lab.menus.writing import writing_evaluation_prompts as prompts

    app = AppTest.from_file(APP, default_timeout=30).run()
    app.selectbox(key="writing-evaluation-type").set_value(slot).run()
    revised = DEFAULT_SYSTEMS[slot] + "\n개정된 평가용 보조 지침"
    monkeypatch.setattr(prompts, "DEFAULT_SYSTEMS", {**DEFAULT_SYSTEMS, slot: revised})
    app.run()
    assert not app.exception
    assert app.text_area(key=f"evaluation-system-{slot}").value == revised
    app.text_area(key=f"evaluation-system-{slot}").set_value("사용자가 저장한 평가 지침")
    next(button for button in app.button if button.label == "평가 프롬프트 저장").click().run()
    monkeypatch.setattr(prompts, "DEFAULT_SYSTEMS", DEFAULT_SYSTEMS)
    app.run()
    assert not app.exception
    assert app.text_area(key=f"evaluation-system-{slot}").value == "사용자가 저장한 평가 지침"
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    assert evaluation_settings(storage, slot)["system"] == "사용자가 저장한 평가 지침"


def test_workshop_manual_evaluation_human_comparison_and_source_switch(tmp_path):
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="evaluation-load-answer-102:51").click().run()
    request = make_request()
    app.text_area(key="evaluation-manual-102:51").set_value(json.dumps(response_data(request), ensure_ascii=False)).run()
    app.button(key="writing-evaluation-import").click().run()
    assert not app.exception and not app.error
    assert app.metric[0].value == "10 / 10"
    run_id = app.selectbox(key="evaluation-history-51").value
    app.number_input(key=f"evaluation-human-score-{run_id}-a").set_value(4)
    app.number_input(key=f"evaluation-human-score-{run_id}-b").set_value(5)
    app.text_area(key=f"evaluation-human-notes-{run_id}").set_value("사람 채점 근거")
    next(button for button in app.button if button.label == "사람 채점 저장").click().run()
    assert not app.exception
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    assert human_review(storage, run_id)["scores"] == {"a": 4, "b": 5}
    app.button(key="writing-evaluation-import").click().run()
    assert not app.exception
    assert len(storage.list_runs(OPERATION)) == 2
    assert any("점수 폭" in frame.value.columns for frame in app.dataframe)
    app.selectbox(key="evaluation-reference-51").set_value("60:51").run()
    assert not app.exception
    assert app.metric[0].value == "10 / 10"


def test_workshop_saves_prompt_and_rejects_a_response_to_previous_prompt(tmp_path):
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.button(key="evaluation-load-answer-102:51").click().run()
    old_response = response_data(make_request())
    app.text_area(key="evaluation-notes-51").set_value("문맥상 가능한 대체 표현을 검토한다.")
    next(button for button in app.button if button.label == "평가 프롬프트 저장").click().run()
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    assert evaluation_settings(storage, 51)["notes"] == "문맥상 가능한 대체 표현을 검토한다."
    app.text_area(key="evaluation-manual-102:51").set_value(json.dumps(old_response, ensure_ascii=False)).run()
    app.button(key="writing-evaluation-import").click().run()
    assert not app.exception
    assert app.error and not app.metric
    assert json.loads(storage.list_runs(OPERATION)[0]["result_json"])["error"]
