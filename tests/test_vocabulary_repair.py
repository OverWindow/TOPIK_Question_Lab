from copy import deepcopy
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from test_vocabulary_questions import CONFIG, WORD, FakeBank, bank, bundle, finish, result, review
from topik_question_lab.common.vocabulary_models import balance_answer_positions, digest, merge_repair, validate_bundle
from topik_question_lab.step_02_planning.vocabulary_prompts import prompt_config
from topik_question_lab.step_03_generation.vocabulary_bulk import VocabularyRunner
from topik_question_lab.step_04_review.vocabulary_validation import apply_review


def reviewed_candidate(kwargs):
    return json.loads(kwargs["user_prompt"].split("검수할 묶음: ", 1)[1])


def repair_response(kwargs):
    prompt = kwargs["user_prompt"]
    candidate = json.loads(prompt.split("기존 묶음: ", 1)[1].split("\n수정 대상 position:", 1)[0])
    positions = json.loads(prompt.split("수정 대상 position: ", 1)[1].split("\n", 1)[0])
    questions = [q for q in candidate["questions"] if q["position"] in positions]
    for q in questions:
        q["explanation"] += " 검수 지적을 반영한 수정입니다."
    return {"questions": questions}


def repair_caller(calls, *, final_failed=(), pause_at=None, stop=None):
    def invoke(**kwargs):
        calls.append(kwargs)
        count = len(calls)
        if count == 1:
            payload = bundle()
        elif count == 3:
            payload = repair_response(kwargs)
        else:
            payload = review(reviewed_candidate(kwargs), failed=(2, 5) if count == 2 else final_failed)
        if count == pause_at:
            stop()
        return result(payload, kwargs["operation"])
    return invoke


def test_four_calls_preserve_passed_questions_and_final_review_covers_whole_bundle(tmp_path):
    bank, calls = FakeBank(), []
    custom = {**CONFIG, "repair_prompt": "사용자 수정 지침"}
    runner = VocabularyRunner(tmp_path, repair_caller(calls), lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=custom)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "saved" and task["repair_count"] == 1
    assert [call["operation"] for call in calls] == ["generation", "vocabulary_review", "generation", "vocabulary_review"]
    before, after = reviewed_candidate(calls[1]), reviewed_candidate(calls[3])
    assert len(after["questions"]) == 7
    for original, final in zip(before["questions"], after["questions"]):
        if original["position"] in (2, 5):
            assert final["explanation"].endswith("검수 지적을 반영한 수정입니다.")
            assert final["correct_answer"] == original["correct_answer"]
        else:
            assert final == original
    assert calls[2]["user_prompt"].startswith("사용자 수정 지침")
    assert "2번: 복수 정답 가능" in calls[2]["user_prompt"]
    assert "수정 대상 position: [2, 5]" in calls[2]["user_prompt"]
    checkpoint = task["checkpoint"]
    assert [call["phase"] for call in checkpoint["calls"]] == ["generate", "review", "repair", "final_review"]
    assert checkpoint["candidate"] == after and checkpoint["reviewed_hash"] == digest(after)
    assert not checkpoint["issues"] and not checkpoint["failed_positions"]
    assert not checkpoint["initial_review"]["passed"]
    assert bank.commits[task["generation_id"]] == after


@pytest.mark.parametrize("pause_at", [2, 3])
def test_stop_during_repair_archives_attempt_and_next_run_starts_from_generation(tmp_path, pause_at):
    bank, calls = FakeBank(), []
    runner = VocabularyRunner(tmp_path, bank_factory=lambda _: bank)
    runner.provider_call = repair_caller(calls, pause_at=pause_at, stop=runner.stop)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    old = runner.store.tasks(batch_id)[0]
    assert len(calls) == pause_at and old["repair_count"] == 0
    assert old["stage"] == "generate" and old["status"] == "pending"
    assert old["checkpoint"]["last_attempt"]["repair_count"] == 1
    next_calls = []
    new_runner = VocabularyRunner(tmp_path, repair_caller(next_calls), lambda _: bank)
    next_id = new_runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(new_runner)
    assert new_runner.store.tasks(next_id)[0]["status"] == "saved" and len(next_calls) == 4
    assert next_calls[0]["operation"] == "generation"
    assert new_runner.store.task(old["generation_id"]) == old


@pytest.mark.parametrize("stage", ["repair", "review"])
def test_recovery_resets_unfinished_steps_and_preserves_returned_response(tmp_path, stage):
    bank, calls = FakeBank(), []
    runner = VocabularyRunner(tmp_path, repair_caller(calls), lambda _: bank)
    batch_id = runner.store.create(bank.identity(), {**CONFIG, **prompt_config()}, [bank.snapshot(1)])
    task = runner.store.tasks(batch_id)[0]
    pending = result(review(bundle()) if stage == "review" else {"questions": [bundle()["questions"][1]]}).model_dump(mode="json")
    runner.store.update(task["generation_id"], stage=stage, status="processing", repair_count=1, attempts=1,
                        checkpoint={"candidate": bundle(), "pending_response": pending})
    runner.recover_idle()
    archived = runner.store.task(task["generation_id"])
    assert not calls and archived["stage"] == "generate" and archived["repair_count"] == archived["attempts"] == 0
    assert archived["checkpoint"]["pending_response"] == pending
    assert archived["checkpoint"]["last_attempt"]["stage"] == stage


@pytest.mark.parametrize("failure", ["missing", "extra", "duplicate", "invalid_answer", "type", "bundle_metadata", "duplicate_stem"])
def test_bad_repair_stops_without_overwriting_candidate_or_final_review(tmp_path, failure):
    bank, calls = FakeBank(), []
    def invoke(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return result(bundle())
        if len(calls) == 2:
            return result(review(reviewed_candidate(kwargs), failed=(2,)), kwargs["operation"])
        fixed = repair_response(kwargs)
        question = fixed["questions"][0]
        if failure == "missing":
            fixed["questions"] = []
        elif failure == "extra":
            fixed["questions"].append(bundle()["questions"][0])
        elif failure == "duplicate":
            fixed["questions"].append(deepcopy(question))
        elif failure == "invalid_answer":
            question["accepted_answers"] = []
        elif failure == "type":
            question["question_type"] = "conjugation"
        elif failure == "bundle_metadata":
            fixed["target_meaning"] = "다른 의미"
        else:
            question["stem"] = bundle()["questions"][0]["stem"]
        return result(fixed)
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert len(calls) == 3 and task["status"] == "pending" and task["repair_count"] == 0
    assert task["checkpoint"]["last_attempt"]["repair_count"] == 1
    assert task["error"].startswith("수정 응답 형식 오류")
    assert task["checkpoint"]["candidate"] == reviewed_candidate(calls[1])
    assert "reviewed_hash" not in task["checkpoint"] and not bank.commits
    assert len(calls) == 3


def test_answer_reordering_preserves_balance_and_answer_text():
    original = balance_answer_positions(validate_bundle(bundle(), WORD), seed="balance").model_dump()
    changed = deepcopy(original["questions"][1])
    answer = changed["choices"].pop(changed["correct_answer"] - 1)
    changed["choices"].insert(0, answer)
    changed["correct_answer"] = 1
    fixed = merge_repair({"questions": [changed]}, original, WORD, [2])
    assert fixed == original


def test_review_warnings_do_not_trigger_repair_and_real_errors_still_block(tmp_path):
    bank, calls = FakeBank(), []
    candidate = bundle(relation=False)
    warnings = ["선택 문항 7번은 필요하면 추가할 수 있습니다."]
    checked = review(candidate)
    checked["warnings"] = warnings
    checked["questions"][0]["warnings"] = ["허용 답안의 드문 표현은 추가 제안입니다."]
    def invoke(**kwargs):
        calls.append(kwargs)
        return result(checked if kwargs["operation"] == "vocabulary_review" else candidate, kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert len(calls) == 2 and task["status"] == "saved" and task["repair_count"] == 0
    assert task["checkpoint"]["review"]["warnings"][0] == warnings[0]
    checked["questions"][1]["issues"] = ["비용과 시간이 모두 정답이 될 수 있습니다."]
    _, verdict, failed = apply_review(checked, candidate, WORD)
    assert not verdict["passed"] and failed == [2]


def test_blank_markers_normalize_without_accepting_multiple_or_missing_blanks():
    for marker in ("_____", "（　）", "(    )"):
        candidate = bundle()
        candidate["questions"][0]["stem"] = f"비가 와서 운동회를 {marker}."
        assert validate_bundle(candidate, WORD).questions[0].stem == "비가 와서 운동회를 ( )."
    for stem in ("빈칸 없음", "_____와 _____", "( )와 _____"):
        candidate = bundle()
        candidate["questions"][0]["stem"] = stem
        with pytest.raises(ValueError):
            validate_bundle(candidate, WORD)


def test_final_failure_resets_budget_for_a_new_four_call_attempt(tmp_path):
    bank, calls = FakeBank(), []
    runner = VocabularyRunner(tmp_path, repair_caller(calls, final_failed=(2,)), lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    old = runner.store.tasks(batch_id)[0]
    assert old["status"] == "pending" and old["stage"] == "generate" and old["repair_count"] == 0
    assert len(calls) == 4 and not bank.commits
    next_calls = []
    runner.provider_call = repair_caller(next_calls)
    next_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    assert len(next_calls) == 4 and runner.store.tasks(next_id)[0]["status"] == "saved"
    assert runner.store.task(old["generation_id"]) == old


def test_malformed_final_review_resets_to_generation_without_automatic_repeat(tmp_path):
    bank, calls = FakeBank(), []
    normal = repair_caller(calls)
    def invoke(**kwargs):
        response = normal(**kwargs)
        return result({"issues": [], "questions": []}, kwargs["operation"]) if len(calls) == 4 else response
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "pending" and task["stage"] == "generate" and task["repair_count"] == 0
    assert "reviewed_hash" not in task["checkpoint"] and not bank.commits and len(calls) == 4
    assert task["checkpoint"]["review_errors"]


@pytest.mark.parametrize("retry_stage", ["repair", "final"])
def test_transient_error_retries_current_step_without_another_repair(tmp_path, monkeypatch, retry_stage):
    from topik_question_lab.step_03_generation.vocabulary_bulk import RateGate
    monkeypatch.setattr(RateGate, "wait", lambda *a: None)
    bank, calls, good_calls = FakeBank(), [], []
    normal = repair_caller(good_calls)
    def invoke(**kwargs):
        calls.append(kwargs)
        if len(calls) == (3 if retry_stage == "repair" else 4):
            return result(None, kwargs["operation"], error="temporary", error_retryable=True)
        return normal(**kwargs)
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "saved" and task["repair_count"] == 1
    assert len(calls) == 5 and len(good_calls) == 4
    index = 2 if retry_stage == "repair" else 3
    assert calls[index]["user_prompt"] == calls[index + 1]["user_prompt"]


def test_old_workflow_is_archived_without_running_or_overwriting_recorded_prompts(tmp_path):
    bank, calls = FakeBank(), []
    runner = VocabularyRunner(tmp_path, repair_caller(calls), lambda _: bank)
    config = {**CONFIG, **prompt_config(), "prompt_version": "vocabulary-5"}
    for key in ("repair_on_failure", "repair_prompt", "repair_schema"):
        config.pop(key)
    batch_id = runner.store.create(bank.identity(), config, [bank.snapshot(1)])
    task = runner.store.tasks(batch_id)[0]
    runner.store.update(task["generation_id"], stage="review", checkpoint={"candidate": bundle()})
    runner.recover_idle()
    task = runner.store.tasks(batch_id)[0]
    assert not calls and task["status"] == "pending" and task["stage"] == "generate"
    assert task["checkpoint"]["candidate"] == bundle()
    assert runner.store.batch(batch_id)["config"] == config


def test_repaired_bundle_is_auto_saved_after_final_review(bank, tmp_path, monkeypatch):
    from topik_question_lab.ui import vocabulary_questions_ui as ui
    calls = []
    runner = VocabularyRunner(tmp_path, repair_caller(calls))
    batch_id = runner.start(bank.url, snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    monkeypatch.setenv("DATABASE_URL", bank.url)
    monkeypatch.setattr(ui, "get_vocabulary_runner", lambda _: runner)
    page = Path(__file__).resolve().parents[1] / "topik_question_lab/ui/vocabulary_questions_app.py"
    app = AppTest.from_file(str(page), default_timeout=20).run()
    assert not app.exception and not any(b.key in {"vocab-save", "vocab-resume"} for b in app.button)
    assert len(bank.snapshot(1)["existing"]) == 7
    assert [m.value for m in app.metric] == ["1", "1", "0"]
    history = app.selectbox(key=f"vocab-call-{task['generation_id']}")
    assert history.options == ["1. 최초 생성", "2. 1차 검수", "3. 지적 문항 수정", "4. 최종 검수"]
    assert not app.exception and len(calls) == 4
    saved = bank.snapshot(1)["existing"]
    for before, after in zip(task["checkpoint"]["candidate"]["questions"], saved):
        assert all(before[key] == after[key] for key in before)


def test_existing_prompt_draft_is_preserved_and_can_restore_new_defaults(bank, monkeypatch):
    from topik_question_lab.step_02_planning.vocabulary_prompts import SYSTEM, REPAIR
    monkeypatch.setenv("DATABASE_URL", bank.url)
    page = Path(__file__).resolve().parents[1] / "topik_question_lab/ui/vocabulary_questions_app.py"
    app = AppTest.from_file(str(page), default_timeout=20)
    draft_key = f"vocab-prompt-draft-{bank.identity()}"
    prefix = f"vocab-prompt-{bank.identity()}"
    app.session_state[draft_key] = {"system": "기존 사용자 지침", "generation_prompt": "기존 생성 지침",
                                    "review_prompt": "기존 검수 지침"}
    app.session_state["vocab-target"] = "missing"
    app.run()
    assert not app.exception and any("이전 프롬프트 초안" in notice.value for notice in app.info)
    assert app.selectbox(key="vocab-target").value == "unfinished"
    assert app.text_area(key=f"{prefix}-system").value == "기존 사용자 지침"
    assert app.text_area(key=f"{prefix}-repair_prompt").value == REPAIR
    app.button(key=f"{prefix}-reset").click().run()
    assert not app.exception and not any("이전 프롬프트 초안" in notice.value for notice in app.info)
    assert app.text_area(key=f"{prefix}-system").value == SYSTEM
