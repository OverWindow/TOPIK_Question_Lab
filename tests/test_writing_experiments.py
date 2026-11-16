import json

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.common import writing_experiments as lab
from topik_question_lab.common.generation.providers import manual_result
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.writing_evaluation import LearnerAnswer, SCORE_LIMITS
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing.writing_evaluation_prompts import DEFAULT_SYSTEMS, load_answer_references, rubric_for
from topik_question_lab.menus.writing.writing_pipeline import load_references
from topik_question_lab.menus.writing.writing_settings import save_generation_default


CHOICE = lab.ModelChoice(provider="test", model="fixture")
APP = str(PACKAGE_ROOT / "menus/writing/writing_evaluation_app.py")


def create(storage, slot=53):
    refs = load_references()
    reference = next(r for r in refs if r.source_key == f"60:{slot}")
    return lab.create_experiment(storage, reference, load_answer_references(refs)[reference.source_key], rubric_for(slot),
        name="합성 실험", per_level=1, generator=CHOICE, prior=CHOICE)


def add_round(storage, experiment_id, slot=53, repeats=2, notes=""):
    return lab.create_round(storage, experiment_id, evaluator=CHOICE, system=DEFAULT_SYSTEMS[slot],
                            notes=notes, rubric=rubric_for(slot), repeats=repeats, name="평가 실험")


@pytest.fixture
def fake_models(monkeypatch):
    calls = []

    def call(provider, model, operation, system, user, **kwargs):
        payload = json.loads(user)
        calls.append({"operation": operation, "system": system, "payload": payload, "kwargs": kwargs})
        slot = payload["question"]["question"]["type_slot"]
        number = len(calls)
        if operation == "writing_answer_generation":
            answer = ({"a": f"답안 {number}을 정리해야 합니다", "b": f"일정 {number}이 시작되기 전에"} if slot < 53
                      else {"text": f"실험용 답안 {number}이다. 자료에 나타난 내용을 설명하는 문장이다."})
            data = {"request_id": payload["request_id"], "answer": answer, "design_notes": "DESIGN_SENTINEL 생성 의도"}
        elif operation == "writing_prior_assessment":
            data = {"request_id": payload["request_id"], "level": "보통",
                "scores": {key: maximum - 2 for key, maximum in SCORE_LIMITS[slot].items()},
                "reasons": {key: "PRIOR_SENTINEL 사전 판단 근거" for key in SCORE_LIMITS[slot]}, "uncertainties": []}
        else:
            original = payload["answer"]
            data = {"request_id": payload["request_id"], "type_slot": slot,
                "scores": {key: {"score": maximum, "rationale": "모의 평가 근거",
                    "quotes": [original[key] if slot < 53 else original["text"]], "parts": {}}
                    for key, maximum in SCORE_LIMITS[slot].items()},
                "task_checks": [{"requirement_id": key, "status": "충족", "reason": "모의 과제 수행",
                    "quotes": [original[key] if slot < 53 else original["text"]]} for key in payload["tasks"]],
                "summary": "전송·저장 검증용 모의 응답", "corrections": [], "review_reasons": []}
        return manual_result(provider, model, operation, json.dumps(data, ensure_ascii=False))

    monkeypatch.setattr(lab, "call_provider", call)
    monkeypatch.setattr("topik_question_lab.common.writing_evaluation.call_provider", call)
    return calls


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_full_experiment_keeps_prediction_blind_and_resumes_without_duplicate_calls(tmp_path, fake_models, slot):
    storage = WritingStorage(tmp_path / "lab.db")
    with storage.connect() as connection:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    experiment_id = create(storage, slot)
    assert not fake_models
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    round_id = add_round(storage, experiment_id, slot)
    lab.run_pending(storage, experiment_id, "evaluation", round_id)
    assert len(fake_models) == 20
    # Reopen the database and resume the same completed work.
    storage = WritingStorage(storage.path)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    lab.run_pending(storage, experiment_id, "evaluation", round_id)
    assert len(fake_models) == 20
    experiment = lab.load_experiment(storage, experiment_id)
    runs = lab.experiment_runs(storage, experiment_id)
    assert len(runs) == 20
    assert len({lab.digest(case["answer"]) for case in experiment["cases"]}) == 5
    assert all(case["assessment"]["level"] == "보통" for case in experiment["cases"])
    rows = lab.comparison_rows(experiment, runs, round_id)
    assert all(row["완료"] == 2 and row["점수 폭"] == 0 for row in rows)
    assert all(row["차이(실제−예상)"] == (4 if slot < 53 else 6) for row in rows)
    for call in fake_models:
        assert call["kwargs"]["managed_retries"] is True
        assert call["kwargs"]["request_timeout"] == 120
        if call["operation"] != "writing_answer_generation":
            encoded = json.dumps(call["payload"], ensure_ascii=False)
            assert "DESIGN_SENTINEL" not in encoded and "goal_level" not in call["payload"]
            assert "profile" not in call["payload"]
            assert "reference_answer" not in call["payload"]
            assert "참고답안은 제공되지 않는다" in call["system"]
        if call["operation"] == "writing_evaluation":
            assert call["system"] == DEFAULT_SYSTEMS[slot]
            assert "PRIOR_SENTINEL" not in json.dumps(call["payload"], ensure_ascii=False)
            assert "assessment" not in call["payload"] and "scores" not in call["payload"]
    assert not storage.list_generated()
    with storage.connect() as connection:
        assert [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")] == tables
        assert connection.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'writing:evaluation:human:%'").fetchone()[0] == 0


def test_edit_then_freeze_and_new_round_use_same_answers_and_saved_prompts(tmp_path, fake_models):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    with pytest.raises(ValueError, match="모든 답안"):
        lab.run_next(storage, experiment_id, "prior")
    lab.run_pending(storage, experiment_id, "generation")
    lab.edit_answer(storage, experiment_id, "A001", LearnerAnswer(text="사용자가 수정한 합성 답안"))
    lab.run_pending(storage, experiment_id, "prior")
    with pytest.raises(ValueError, match="고정"):
        lab.edit_answer(storage, experiment_id, "A001", LearnerAnswer(text="다른 답안"))
    first = add_round(storage, experiment_id, repeats=1, notes="FIRST_PROMPT")
    lab.run_pending(storage, experiment_id, "evaluation", first)
    second = add_round(storage, experiment_id, repeats=1, notes="SECOND_PROMPT")
    lab.run_pending(storage, experiment_id, "evaluation", second)
    experiment = lab.load_experiment(storage, experiment_id)
    assert experiment["rounds"][0]["notes"] == "FIRST_PROMPT"
    assert experiment["rounds"][1]["notes"] == "SECOND_PROMPT"
    evaluations = [call["payload"] for call in fake_models if call["operation"] == "writing_evaluation"]
    first_answers = {lab.digest(p["answer"]) for p in evaluations[:5]}
    assert first_answers == {lab.digest(p["answer"]) for p in evaluations[5:]}
    assert any(p["answer"]["text"] == "사용자가 수정한 합성 답안" for p in evaluations)
    assert len([c for c in fake_models if c["operation"] == "writing_prior_assessment"]) == 5


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_legacy_scoring_stages_are_read_only_and_new_round_reuses_answers_without_references(tmp_path, fake_models, slot):
    from copy import deepcopy

    storage = WritingStorage(tmp_path / "legacy.db")
    experiment_id = create(storage, slot)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    old_round = add_round(storage, experiment_id, slot, repeats=1)
    lab.run_pending(storage, experiment_id, "evaluation", old_round)
    with storage.connect() as connection:
        experiment = lab._read(connection, experiment_id)
        experiment.pop("prior_input_version")
        experiment["rounds"][0].pop("evaluation_input_version")
        experiment["rounds"][0]["tasks"][0]["status"] = "failed"
        lab._write(connection, experiment)
    before = deepcopy(lab.load_experiment(storage, experiment_id))
    old_runs = lab.experiment_runs(storage, experiment_id, round_ids=[old_round])
    calls_before = len(fake_models)
    for stage, round_id in (("prior", None), ("evaluation", old_round)):
        assert lab.legacy_scoring_stage(before, stage, round_id)
        with pytest.raises(ValueError, match="조회만"):
            lab.build_stage_request(before, before["cases"][0], stage, round_id)
        with pytest.raises(ValueError, match="조회만"):
            lab.retry_failed(storage, experiment_id, stage, round_id)
    assert len(fake_models) == calls_before
    assert lab.load_experiment(storage, experiment_id) == before

    new_round = add_round(storage, experiment_id, slot, repeats=1)
    updated = lab.load_experiment(storage, experiment_id)
    assert not lab.legacy_scoring_stage(updated, "evaluation", new_round)
    # Scoring remains usable even if the generation-only reference is unavailable.
    without_reference = deepcopy(updated)
    without_reference.pop("answer_reference")
    request = lab.build_stage_request(without_reference, updated["cases"][0], "evaluation", new_round)
    assert "reference_answer" not in request.payload
    assert request.payload["answer"] == LearnerAnswer.model_validate(before["cases"][0]["answer"]).for_slot(slot)
    lab.run_pending(storage, experiment_id, "evaluation", new_round)
    after = lab.load_experiment(storage, experiment_id)
    assert after["cases"] == before["cases"] and after["rounds"][0] == before["rounds"][0]
    assert lab.experiment_runs(storage, experiment_id, round_ids=[old_round]) == old_runs
    assert all("reference_answer" not in call["payload"] for call in fake_models[calls_before:])


def test_new_prior_request_does_not_require_generation_reference(tmp_path, fake_models):
    storage = WritingStorage(tmp_path / "prior.db")
    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    experiment = lab.load_experiment(storage, experiment_id)
    experiment.pop("answer_reference")
    request = lab.build_stage_request(experiment, experiment["cases"][0], "prior")
    assert "reference_answer" not in request.payload
    assert request.system == lab.PRIOR_SYSTEM


@pytest.mark.parametrize("stage", ["prior", "evaluation"])
def test_legacy_pending_scoring_cannot_claim_or_call_model(tmp_path, fake_models, stage):
    storage = WritingStorage(tmp_path / "legacy-pending.db")
    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    round_id = None
    if stage == "evaluation":
        lab.run_pending(storage, experiment_id, "prior")
        round_id = add_round(storage, experiment_id)
    with storage.connect() as connection:
        experiment = lab._read(connection, experiment_id)
        experiment.pop("prior_input_version")
        if round_id:
            experiment["rounds"][0].pop("evaluation_input_version")
        lab._write(connection, experiment)
    before = lab.load_experiment(storage, experiment_id)
    calls = len(fake_models)
    with pytest.raises(ValueError, match="조회만"):
        lab.run_next(storage, experiment_id, stage, round_id)
    assert lab.load_experiment(storage, experiment_id) == before
    assert len(fake_models) == calls


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_type_default_only_applies_to_new_rounds_and_old_retries_keep_snapshot(tmp_path, fake_models, monkeypatch, slot):
    from copy import deepcopy
    from topik_question_lab.common import writing_evaluation
    from topik_question_lab.menus.writing import writing_evaluation_prompts as prompts

    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage, slot)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    monkeypatch.setattr(prompts, "DEFAULT_SYSTEMS", {**DEFAULT_SYSTEMS, slot: "이전에 사용한 평가 프롬프트"})
    old_settings = prompts.evaluation_settings(storage, slot)
    old_round = lab.create_round(storage, experiment_id, evaluator=CHOICE, system=old_settings["system"],
        notes=old_settings["notes"], rubric=rubric_for(slot), repeats=1, name="기존 차수")
    normal = writing_evaluation.call_provider
    def fail(*args, **kwargs):
        return normal(*args, **kwargs).model_copy(update={"error": "한 번의 모의 실패"})
    monkeypatch.setattr(writing_evaluation, "call_provider", fail)
    lab.run_next(storage, experiment_id, "evaluation", old_round)
    monkeypatch.setattr(writing_evaluation, "call_provider", normal)
    lab.run_pending(storage, experiment_id, "evaluation", old_round)
    before = deepcopy(lab.load_experiment(storage, experiment_id))
    old_runs = lab.experiment_runs(storage, experiment_id, round_ids=[old_round])
    old_request = lab.build_stage_request(before, before["cases"][0], "evaluation", old_round)

    monkeypatch.setattr(prompts, "DEFAULT_SYSTEMS", DEFAULT_SYSTEMS)
    settings = prompts.evaluation_settings(storage, slot)
    new_round = lab.create_round(storage, experiment_id, evaluator=CHOICE, system=settings["system"],
        notes=settings["notes"], rubric=rubric_for(slot), repeats=1, name="새 평가 차수")
    lab.run_pending(storage, experiment_id, "evaluation", new_round)
    after = lab.load_experiment(storage, experiment_id)
    assert after["rounds"][0] == before["rounds"][0]
    assert after["cases"] == before["cases"]
    assert after["generation_system"] == before["generation_system"]
    assert after["prior_system"] == before["prior_system"]
    assert lab.build_stage_request(after, after["cases"][0], "evaluation", old_round) == old_request
    new_request = lab.build_stage_request(after, after["cases"][0], "evaluation", new_round)
    assert new_request.system == DEFAULT_SYSTEMS[slot] and new_request.request_id != old_request.request_id
    assert "assessment" not in new_request.payload and "goal_level" not in new_request.payload
    assert lab.experiment_runs(storage, experiment_id, round_ids=[old_round]) == old_runs

    calls_before = len(fake_models)
    lab.retry_failed(storage, experiment_id, "evaluation", old_round)
    lab.run_pending(storage, experiment_id, "evaluation", old_round)
    assert len(fake_models) == calls_before + 1
    assert fake_models[-1]["system"] == old_settings["system"]
    preserved = lab.experiment_runs(storage, experiment_id, round_ids=[old_round])
    assert all(preserved[run_id] == run for run_id, run in old_runs.items())
    assert all(run["prompt_system"] == DEFAULT_SYSTEMS[slot]
               for run in lab.experiment_runs(storage, experiment_id, round_ids=[new_round]).values())


def test_failure_is_saved_and_only_failed_task_is_retried(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    normal = lab.call_provider
    failed_once = False
    def fail_one(*args, **kwargs):
        nonlocal failed_once
        result = normal(*args, **kwargs)
        if not failed_once:
            failed_once = True
            return result.model_copy(update={"parsed_json": {"request_id": "wrong"}})
        return result
    monkeypatch.setattr(lab, "call_provider", fail_one)
    lab.run_pending(storage, experiment_id, "generation")
    experiment = lab.load_experiment(storage, experiment_id)
    assert [c["generation"]["status"] for c in experiment["cases"]].count("failed") == 1
    assert len(fake_models) == 5 and len(lab.experiment_runs(storage, experiment_id)) == 5
    lab.run_pending(storage, experiment_id, "generation")
    assert len(fake_models) == 5
    lab.retry_failed(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "generation")
    assert len(fake_models) == 6
    assert all(c["answer"] for c in lab.load_experiment(storage, experiment_id)["cases"])


def test_duplicate_answer_and_bad_prior_are_not_accepted(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    experiment = lab.load_experiment(storage, experiment_id)
    with pytest.raises(ValueError, match="동일한"):
        lab.edit_answer(storage, experiment_id, "A002", LearnerAnswer.model_validate(experiment["cases"][0]["answer"]))
    normal = lab.call_provider
    def wrong_prior(*args, **kwargs):
        result = normal(*args, **kwargs)
        result.parsed_json["scores"]["content"] = 99
        return result
    monkeypatch.setattr(lab, "call_provider", wrong_prior)
    lab.run_next(storage, experiment_id, "prior")
    first = lab.load_experiment(storage, experiment_id)["cases"][0]
    assert first["prior"]["status"] == "failed" and "assessment" not in first
    with pytest.raises(ValueError, match="모든 답안의 사전 판단"):
        add_round(storage, experiment_id)


def test_duplicate_claim_and_interrupted_call_recovery(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    claimed = lab._claim(storage, experiment_id, "generation", None)
    with pytest.raises(ValueError, match="실행 중"):
        lab.run_next(storage, experiment_id, "generation")
    lab.recover_interrupted(storage, experiment_id)
    assert lab.load_experiment(storage, experiment_id)["cases"][0]["generation"]["status"] == "running"
    monkeypatch.setattr(lab.time, "time", lambda: claimed[2]["started_at"] + lab.LEASE_SECONDS + 1)
    lab.recover_interrupted(storage, experiment_id)
    assert lab.load_experiment(storage, experiment_id)["cases"][0]["generation"]["status"] == "failed"
    assert not fake_models
    lab.retry_failed(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "generation")
    assert len(fake_models) == 5


def test_response_and_manifest_commit_atomically(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    original = lab._write
    writes = 0
    def fail_completion(connection, experiment):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("simulated persistence failure")
        original(connection, experiment)
    monkeypatch.setattr(lab, "_write", fail_completion)
    with pytest.raises(RuntimeError):
        lab.run_next(storage, experiment_id, "generation")
    assert not lab.experiment_runs(storage, experiment_id)
    state = lab.load_experiment(storage, experiment_id)
    assert state["cases"][0]["answer"] is None
    assert state["cases"][0]["generation"]["status"] == "running"


def test_unknown_call_errors_do_not_expose_exception_contents(tmp_path, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-sensitive-exception-details")
    monkeypatch.setattr(lab, "call_provider", fail)
    lab.run_next(storage, experiment_id, "generation")
    assert "synthetic-sensitive" not in json.dumps(lab.experiment_runs(storage, experiment_id))


def test_invalid_actual_score_is_excluded_and_retry_keeps_completed_repeats(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    round_id = add_round(storage, experiment_id)
    from topik_question_lab.common import writing_evaluation
    normal = writing_evaluation.call_provider
    first = True
    def wrong_score(*args, **kwargs):
        nonlocal first
        result = normal(*args, **kwargs)
        if first:
            first = False
            result.parsed_json["scores"]["content"]["score"] = 99
        return result
    monkeypatch.setattr(writing_evaluation, "call_provider", wrong_score)
    lab.run_pending(storage, experiment_id, "evaluation", round_id)
    experiment = lab.load_experiment(storage, experiment_id)
    rows = lab.comparison_rows(experiment, lab.experiment_runs(storage, experiment_id), round_id)
    assert sum(row["실패"] for row in rows) == 1
    assert sum(row["완료"] for row in rows) == 9
    assert all(row["평가 평균"] == 30 for row in rows)
    lab.retry_failed(storage, experiment_id, "evaluation", round_id)
    lab.run_pending(storage, experiment_id, "evaluation", round_id)
    assert len(fake_models) == 21
    rows = lab.comparison_rows(lab.load_experiment(storage, experiment_id), lab.experiment_runs(storage, experiment_id), round_id)
    assert all(row["완료"] == 2 and row["실패"] == 0 for row in rows)


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_bulk_ui_runs_all_stages_and_compares_rounds(tmp_path, fake_models, monkeypatch, slot):
    from topik_question_lab.menus.writing import writing_evaluation_prompts as prompts

    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-key")
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    save_generation_default(storage, "bizrouter:fixture", "fixture")
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.selectbox(key="writing-evaluation-type").set_value(slot).run()
    app.radio(key="writing-evaluation-mode").set_value("대량 실험").run()
    assert not app.exception
    app.number_input(key="experiment-per-level").set_value(1).run()
    app.button(key="experiment-create").click().run()
    assert not app.exception and not fake_models
    experiment_id = app.selectbox(key=f"experiment-selected-102:{slot}").value
    assert app.text_area(key=f"experiment-system-{experiment_id}").value == DEFAULT_SYSTEMS[slot]
    app.button(key=f"experiment-{experiment_id}-generation-None-run").click().run()
    assert not app.exception and len(fake_models) == 5
    app.button(key=f"experiment-{experiment_id}-prior-None-run").click().run()
    assert not app.exception and len(fake_models) == 10
    app.number_input(key=f"experiment-repeats-{experiment_id}").set_value(2).run()
    app.button(key="experiment-add-round").click().run()
    assert not app.exception and len(fake_models) == 10
    round_id = app.selectbox(key=f"experiment-round-{experiment_id}").value
    app.button(key=f"experiment-{experiment_id}-evaluation-{round_id}-run").click().run()
    assert not app.exception and not app.error and len(fake_models) == 20
    assert all(c["system"] == DEFAULT_SYSTEMS[slot] for c in fake_models if c["operation"] == "writing_evaluation")
    assert any("차이(실제−예상)" in frame.value.columns for frame in app.dataframe)
    assert any(metric.value == ("4.00점" if slot < 53 else "6.00점") for metric in app.metric)
    revised = DEFAULT_SYSTEMS[slot] + "\n새 비교용 평가 지침"
    monkeypatch.setattr(prompts, "DEFAULT_SYSTEMS", {**DEFAULT_SYSTEMS, slot: revised})
    app.run()
    assert not app.exception
    assert app.text_area(key=f"experiment-system-{experiment_id}").value == revised
    app.text_area(key=f"experiment-notes-{experiment_id}").set_value("새 비교 지침").run()
    app.button(key="experiment-add-round").click().run()
    assert not app.exception
    second_id = app.selectbox(key=f"experiment-round-{experiment_id}").value
    assert second_id != round_id
    app.button(key=f"experiment-{experiment_id}-evaluation-{second_id}-run").click().run()
    assert not app.exception and len(fake_models) == 30
    assert all(c["system"] == revised for c in fake_models[-10:])
    rounds = lab.load_experiment(storage, experiment_id)["rounds"]
    assert rounds[0]["system"] == DEFAULT_SYSTEMS[slot] and rounds[1]["system"] == revised
    reopened = AppTest.from_file(APP, default_timeout=30).run()
    reopened.selectbox(key="writing-evaluation-type").set_value(slot).run()
    reopened.radio(key="writing-evaluation-mode").set_value("대량 실험").run()
    assert not reopened.exception
    assert len(lab.load_experiment(storage, experiment_id)["rounds"]) == 2


MANAGER_APP = str(PACKAGE_ROOT / "menus/writing_experiments/writing_experiments_app.py")


def test_manager_summaries_preserve_legacy_records_and_show_stale_and_failed(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    pending_id = create(storage, 51)
    ready_id = create(storage)
    lab.run_pending(storage, ready_id, "generation")
    lab.run_pending(storage, ready_id, "prior")
    assert {s["id"]: s["status"] for s in lab.experiment_summaries(storage)} == {
        pending_id: "대기", ready_id: "평가 준비 완료"}
    round_id = add_round(storage, ready_id)
    lab.run_pending(storage, ready_id, "evaluation", round_id)
    assert next(s for s in lab.experiment_summaries(storage) if s["id"] == ready_id)["status"] == "평가 완료"
    with storage.connect() as connection:
        experiment = lab._read(connection, pending_id)
        experiment["cases"][0]["generation"].update(status="running", started_at=100)
        experiment["cases"][1]["generation"].update(status="failed", error="모의 실패")
        lab._write(connection, experiment)
    monkeypatch.setattr(lab.time, "time", lambda: 701)
    before = lab.load_experiment(storage, pending_id)
    summary = next(s for s in lab.experiment_summaries(storage) if s["id"] == pending_id)
    assert summary["status"] == "실행 상태 확인 필요" and summary["stage"] == "답안 생성"
    assert summary["stale"] == 1 and summary["totals"] == {"pending": 8, "failed": 1, "running": 1}
    assert summary["memo"] == "" and summary["archived"] == 0
    assert "cases" not in summary and "reference" not in summary
    assert lab.load_experiment(storage, pending_id) == before


def test_metadata_edits_preserve_active_task_and_restore_archive(tmp_path):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    claimed = lab._claim(storage, experiment_id, "generation", None)
    lab.update_experiment_metadata(storage, experiment_id, name=" 변경한 이름 ", memo=" 검토 필요 ", archived=True)
    saved = lab.load_experiment(storage, experiment_id)
    assert saved["name"] == "변경한 이름" and saved["memo"] == "검토 필요" and saved["archived"]
    assert saved["cases"] == claimed[0]["cases"]
    with pytest.raises(ValueError, match="이름"):
        lab.update_experiment_metadata(storage, experiment_id, name=" ", memo="", archived=False)
    assert lab.load_experiment(storage, experiment_id) == saved
    lab.update_experiment_metadata(storage, experiment_id, name=saved["name"], memo=saved["memo"], archived=False)
    assert not lab.load_experiment(storage, experiment_id)["archived"]


def test_manager_filters_include_all_model_roles_and_korean_dates(tmp_path):
    from datetime import date
    from topik_question_lab.menus.writing_experiments.writing_experiments_ui import filter_summaries

    storage = WritingStorage(tmp_path / "lab.db")
    first = create(storage, 51)
    second = create(storage, 54)
    lab.update_experiment_metadata(storage, first, name="Alpha 비교", memo="메모 검색", archived=True)
    with storage.connect() as connection:
        experiment = lab._read(connection, first)
        experiment["created_at"] = "2026-11-14T16:00:00+00:00"
        experiment["prior_model"]["model"] = "prior-only"
        experiment["cases"][0]["generation"].update(status="failed", error="오류")
        lab._write(connection, experiment)
    items = lab.experiment_summaries(storage)
    assert [s["id"] for s in filter_summaries(items)] == [second]
    filtered = filter_summaries(items, query="alpha", slot=51, exam=60, status="실패 포함",
        model="test · prior-only", archive="보관만", dates=(date(2026, 11, 15), date(2026, 11, 15)))
    assert [s["id"] for s in filtered] == [first]
    assert not filter_summaries(items, slot=52, archive="보관 포함")
    assert [s["id"] for s in filter_summaries(items, query="메모", archive="보관 포함")] == [first]


def test_round_comparison_uses_shared_completed_cases_and_selected_baseline(tmp_path, fake_models):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    first = add_round(storage, experiment_id, repeats=1)
    lab.run_pending(storage, experiment_id, "evaluation", first)
    second = add_round(storage, experiment_id, repeats=2, notes="두 번째 프롬프트")
    lab.run_pending(storage, experiment_id, "evaluation", second)
    with storage.connect() as connection:
        experiment = lab._read(connection, experiment_id)
        experiment["rounds"][1]["tasks"][0]["status"] = "failed"
        excluded_case = experiment["rounds"][1]["tasks"][0]["case_id"]
        lab._write(connection, experiment)
        for task in experiment["rounds"][1]["tasks"]:
            row = connection.execute("SELECT result_json FROM runs WHERE id=?", (task["run_id"],)).fetchone()
            result = json.loads(row[0])
            result["parsed_json"]["scores"]["content"]["score"] -= 2
            connection.execute("UPDATE runs SET result_json=? WHERE id=?", (json.dumps(result), task["run_id"]))
    runs = lab.experiment_runs(storage, experiment_id, round_ids=[first, second])
    summaries, rows = lab.round_comparison(experiment, runs, [second, first])
    assert [s["차수 ID"] for s in summaries] == [second, first]
    assert [s["평균 점수"] for s in summaries] == [28, 30]
    assert [s["기준 차수와 평균 차이"] for s in summaries] == [0, 2]
    assert [s["기준 차수와 평균 절대 차이"] for s in summaries] == [0, 2]
    assert all(s["공통 답안 수"] == 4 for s in summaries)
    assert len(rows) == 8 and excluded_case not in {r["답안"] for r in rows}
    assert {r["기준 차수와 차이"] for r in rows if r["차수 ID"] == first} == {2}
    assert summaries[0]["완료"] == 9 and summaries[0]["실패"] == 1
    for task in experiment["rounds"][0]["tasks"]:
        task["status"] = "pending"
    summaries, rows = lab.round_comparison(experiment, runs, [first, second])
    assert not rows and all(s["평균 점수"] is None and s["공통 답안 수"] == 0 for s in summaries)


def test_run_queries_scope_and_keep_failed_attempts(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    first = create(storage)
    second = create(storage)
    normal = lab.call_provider
    monkeypatch.setattr(lab, "call_provider", lambda *a, **k: normal(*a, **k).model_copy(update={"error": "모의 실패"}))
    lab.run_next(storage, first, "generation")
    monkeypatch.setattr(lab, "call_provider", normal)
    lab.retry_failed(storage, first, "generation")
    lab.run_next(storage, first, "generation")
    lab.run_next(storage, second, "generation")
    rows = lab.experiment_run_summaries(storage, first)
    assert len(rows) == 2 and rows[0]["case_id"] == rows[1]["case_id"] == "A001"
    assert not rows[0]["error"] and rows[1]["error"] == "모의 실패"
    assert "result_json" not in rows[0] and "prompt_user" not in rows[0]
    assert len(lab.experiment_runs(storage, first)) == 2
    assert lab.experiment_runs(storage, first, round_ids=[]) == {}
    with pytest.raises(ValueError, match="호출 기록"):
        lab.experiment_run(storage, second, rows[0]["id"])


def test_csv_export_preserves_korean_and_escapes_formulas():
    import csv
    import io
    from topik_question_lab.menus.writing_experiments.writing_experiments_ui import csv_bytes

    encoded = csv_bytes([{"실험명": "=1+1", "점수 차이": -2, "근거": "한국어, 줄바꿈\n확인"}])
    assert encoded.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(encoded.decode("utf-8-sig"))))
    assert rows == [{"실험명": "'=1+1", "점수 차이": "-2", "근거": "한국어, 줄바꿈\n확인"}]


def test_manager_list_is_lazy_and_metadata_archive_can_be_restored(tmp_path, monkeypatch):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    first = create(storage)
    create(storage, 51)
    original = lab.load_experiment
    monkeypatch.setattr(lab, "load_experiment", lambda *a: pytest.fail("listing must not load experiment bodies"))
    monkeypatch.setattr(lab, "experiment_runs", lambda *a, **k: pytest.fail("overview must not load model payloads"))
    monkeypatch.setattr(lab, "run_pending", lambda *a, **k: pytest.fail("browsing must not execute"))
    app = AppTest.from_file(MANAGER_APP).run()
    assert not app.exception and app.metric[0].value == "2"
    monkeypatch.setattr(lab, "load_experiment", original)
    app.selectbox(key="writing-manager-selected").set_value(first).run()
    assert not app.exception
    app.radio(key=f"manager-section-{first}").set_value("실험 개요").run()
    app.text_input(key=f"manager-name-{first}").set_value("관리 테스트")
    app.text_area(key=f"manager-memo-{first}").set_value("메모를 남김")
    app.checkbox(key=f"manager-archive-{first}").check()
    next(b for b in app.button if b.label == "관리 정보 저장").click().run()
    assert not app.exception and app.metric[0].value == "1"
    app.selectbox(key="manager-archive").set_value("보관만").run()
    app.selectbox(key="writing-manager-selected").set_value(first).run()
    app.radio(key=f"manager-section-{first}").set_value("실험 개요").run()
    assert app.text_input(key=f"manager-name-{first}").value == "관리 테스트"
    assert app.text_area(key=f"manager-memo-{first}").value == "메모를 남김"
    app.checkbox(key=f"manager-archive-{first}").uncheck()
    next(b for b in app.button if b.label == "관리 정보 저장").click().run()
    assert not app.exception and not lab.load_experiment(storage, first)["archived"]


def test_manager_pagination_and_filter_selection_reset(tmp_path):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    for _ in range(51):
        create(storage)
    app = AppTest.from_file(MANAGER_APP, default_timeout=30).run()
    assert not app.exception and len(app.dataframe[0].value) == 50
    app.selectbox(key="manager-page").set_value(2).run()
    assert len(app.dataframe[0].value) == 1
    experiment_id = app.dataframe[0].value.iloc[0]["실험 ID"]
    app.selectbox(key="writing-manager-selected").set_value(experiment_id).run()
    app.text_input(key="manager-search").set_value(experiment_id).run()
    assert not app.exception and app.selectbox(key="manager-page").value == 1
    app.selectbox(key="manager-slot").set_value(51).run()
    assert not app.exception and app.metric[0].value == "0"


def test_manager_detail_views_and_export_before_rounds(tmp_path, fake_models, monkeypatch):
    import streamlit as st

    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    experiment_id = create(storage)
    lab.run_next(storage, experiment_id, "generation")
    downloads = {}
    original = st.download_button
    def download(label, data, *args, **kwargs):
        downloads[label] = data
        return original(label, data, *args, **kwargs)
    monkeypatch.setattr(st, "download_button", download)
    calls_before = len(fake_models)
    app = AppTest.from_file(MANAGER_APP).run()
    app.selectbox(key="writing-manager-selected").set_value(experiment_id).run()
    app.radio(key=f"manager-section-{experiment_id}").set_value("답안 목록").run()
    assert not app.exception and len(app.dataframe[1].value) == 5
    app.text_input(key=f"manager-answer-search-{experiment_id}").set_value("A002").run()
    assert not app.exception and any("생성되지 않은" in i.value for i in app.info)
    app.radio(key=f"manager-section-{experiment_id}").set_value("평가 차수 비교").run()
    assert not app.exception and any("평가 차수가 없습니다" in i.value for i in app.info)
    app.radio(key=f"manager-section-{experiment_id}").set_value("호출·오류 기록").run()
    run_id = next(iter(lab.experiment_runs(storage, experiment_id)))
    app.selectbox(key=f"manager-call-{experiment_id}").set_value(run_id).run()
    assert not app.exception and any("TOPIK" in code.value for code in app.code)
    app.radio(key=f"manager-section-{experiment_id}").set_value("내보내기").run()
    exported = json.loads(downloads["전체 실험 기록 JSON 받기"])
    assert exported["experiment"]["id"] == experiment_id and not exported["experiment"]["rounds"]
    assert len(exported["runs"]) == 1 and exported["human_reviews"] == {}
    assert len(fake_models) == calls_before and not app.exception


def test_manager_round_view_includes_human_review_and_resources(tmp_path, fake_models):
    from topik_question_lab.common.writing_evaluation import save_human_review

    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    first = add_round(storage, experiment_id, repeats=1)
    lab.run_pending(storage, experiment_id, "evaluation", first)
    second = add_round(storage, experiment_id, repeats=2)
    lab.run_pending(storage, experiment_id, "evaluation", second)
    run_id = next(iter(lab.experiment_runs(storage, experiment_id, round_ids=[first])))
    save_human_review(storage, run_id, {key: maximum - 2 for key, maximum in SCORE_LIMITS[53].items()}, "사람 판단")
    app = AppTest.from_file(MANAGER_APP).run()
    app.selectbox(key="writing-manager-selected").set_value(experiment_id).run()
    app.radio(key=f"manager-section-{experiment_id}").set_value("평가 차수 비교").run()
    assert not app.exception
    frames = [f.value for f in app.dataframe]
    comparison = next(f for f in frames if "공통 답안 수" in f.columns)
    assert comparison["공통 답안 수"].tolist() == [5, 5]
    human = next(f for f in frames if "사람 채점 실행 수" in f.columns)
    assert human["사람 채점 실행 수"].tolist() == [1]
    assert human["사람 점수와 평균 절대 차이"].tolist() == [6]
    resources = next(f for f in frames if "호출 수(재시도 포함)" in f.columns)
    assert resources["호출 수(재시도 포함)"].tolist() == [5, 10]


def test_manager_navigation_handoff_preserves_exact_experiment_and_round(tmp_path, fake_models, monkeypatch):
    from topik_question_lab.common.ui.navigation import menu_page
    from topik_question_lab.common.generation import runner_service

    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    experiment_id = create(storage, 54)
    create(storage, 54)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    first = add_round(storage, experiment_id, slot=54, repeats=1)
    second = add_round(storage, experiment_id, slot=54, repeats=1)
    (tmp_path / "data/bulk_generation.db").touch()
    monkeypatch.setattr(runner_service, "get_runner", lambda *a: pytest.fail("manager must not start runner"))
    calls_before = len(fake_models)
    app = AppTest.from_file(str(PACKAGE_ROOT / "main.py"), default_timeout=30)
    app.switch_page(menu_page("writing_experiments_app.py").relative_to(PACKAGE_ROOT).as_posix()).run()
    app.selectbox(key="writing-manager-selected").set_value(experiment_id).run()
    app.radio(key=f"manager-section-{experiment_id}").set_value("평가 차수 비교").run()
    app.selectbox(key=f"manager-open-round-{experiment_id}").set_value(second).run()
    app.button(key="writing-manager-round-resume").click().run()
    assert not app.exception
    assert app.selectbox(key="writing-evaluation-type").value == 54
    assert app.radio(key="writing-evaluation-mode").value == "대량 실험"
    assert app.selectbox(key="experiment-source-54").value == "60:54"
    assert app.selectbox(key="experiment-selected-60:54").value == experiment_id
    assert app.selectbox(key=f"experiment-round-{experiment_id}").value == second
    assert len(fake_models) == calls_before


def mixed_result_round(storage, monkeypatch):
    from collections import Counter
    from topik_question_lab.common import writing_evaluation

    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    experiment = lab.load_experiment(storage, experiment_id)
    answers = {case["answer"]["text"]: index for index, case in enumerate(experiment["cases"])}
    with storage.connect() as connection:
        experiment["cases"][0]["assessment"]["uncertainties"] = ["사전 판단의 모의 불확실성"]
        lab._write(connection, experiment)
    normal = writing_evaluation.call_provider
    attempts = Counter()

    def evaluate(*args, **kwargs):
        result = normal(*args, **kwargs)
        payload = json.loads(args[4])
        index = answers[payload["answer"]["text"]]
        attempts[index] += 1
        if index > 2 or index == 2 and attempts[index] > 1:
            return result.model_copy(update={"error": "의도한 모의 실패"})
        data = result.parsed_json
        if index == 0:
            data["scores"]["content"]["score"] = 7 if attempts[index] == 3 else 6
        elif index == 1:
            for key, score in {"content": 6 if attempts[index] == 3 else 4, "organization": 4, "language": 10}.items():
                data["scores"][key]["score"] = score
        data["summary"] = "상세 평가 종합 의견"
        data["review_reasons"] = ["추가 확인이 필요한 모의 검토 사유"]
        data["corrections"] = [{"original": payload["answer"]["text"][:8],
                                "suggestion": "더 분명한 표현", "reason": "모의 수정 제안 근거"}]
        return result.model_copy(update={"raw_response": json.dumps(data, ensure_ascii=False)})

    monkeypatch.setattr(writing_evaluation, "call_provider", evaluate)
    round_id = add_round(storage, experiment_id, repeats=3)
    lab.run_pending(storage, experiment_id, "evaluation", round_id)
    return experiment_id, round_id


def test_results_signed_absolute_difference_precision_and_partial_failures(tmp_path, fake_models, monkeypatch):
    storage = WritingStorage(tmp_path / "lab.db")
    experiment_id, round_id = mixed_result_round(storage, monkeypatch)
    experiment = lab.load_experiment(storage, experiment_id)
    runs = lab.experiment_runs(storage, experiment_id, round_ids=[round_id])
    report = lab.round_results(experiment, runs, round_id)
    summary = report["summary"]
    assert summary["완료 답안 수"] == summary["예상 비교 답안 수"] == 2
    assert summary["전체 답안 수"] == 5
    assert summary["평균 사전 예상 점수"] == summary["평균 평가 점수"] == 24
    assert summary["평균 점수 차이"] == pytest.approx(0)
    assert summary["평균 절대 차이"] == pytest.approx(16 / 3)
    assert summary["최대 절대 차이"] == pytest.approx(16 / 3)
    rows = {r["답안"]: r for r in report["answers"]}
    assert rows["A001"]["평가 평균"] == pytest.approx(88 / 3)
    assert rows["A002"]["평가 평균"] == pytest.approx(56 / 3)
    assert rows["A001"]["반복 표준편차"] == pytest.approx((1 / 3) ** .5)
    assert rows["A003"]["평가 평균"] == 30 and rows["A003"]["완료"] == 1 and rows["A003"]["실패"] == 2
    assert not rows["A003"]["요약 포함"] and rows["A003"]["점수 폭"] is None
    assert rows["A004"]["평가 평균"] is None and rows["A004"]["실패"] == 3
    assert len(report["repeats"]) == 15
    assert sum(row["평가 점수"] is not None for row in report["repeats"]) == 7
    assert [r["답안 수"] for r in report["levels"]] == [1, 1]
    assert report["criteria"][0]["평균 차이"] == pytest.approx(.5)
    assert report["criteria"][0]["평균 절대 차이"] == pytest.approx(5 / 6)
    # A second, unfinished round must not hide this round's available results.
    pending = add_round(storage, experiment_id)
    experiment = lab.load_experiment(storage, experiment_id)
    unfinished = lab.round_results(experiment, {}, pending)
    assert unfinished["summary"]["평균 평가 점수"] is None
    assert not unfinished["levels"] and not unfinished["criteria"]
    assert all(r["대기"] == 2 and not r["요약 포함"] for r in unfinished["answers"])
    summaries, shared = lab.round_comparison(experiment, runs, [round_id, pending])
    assert not shared and all(r["공통 답안 수"] == 0 for r in summaries)


@pytest.mark.parametrize("slot", [51, 52, 53, 54])
def test_results_zero_scores_and_single_repeat_are_not_missing(tmp_path, fake_models, monkeypatch, slot):
    import streamlit as st
    from topik_question_lab.common import writing_evaluation

    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    experiment_id = create(storage, slot)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    normal = writing_evaluation.call_provider
    def zero(*args, **kwargs):
        result = normal(*args, **kwargs)
        for score in result.parsed_json["scores"].values():
            score["score"] = 0
            score["parts"] = {key: 0 for key in score["parts"]}
        return result.model_copy(update={"raw_response": json.dumps(result.parsed_json, ensure_ascii=False)})
    monkeypatch.setattr(writing_evaluation, "call_provider", zero)
    round_id = add_round(storage, experiment_id, slot=slot, repeats=1)
    lab.run_pending(storage, experiment_id, "evaluation", round_id)
    experiment = lab.load_experiment(storage, experiment_id)
    report = lab.round_results(experiment, lab.experiment_runs(storage, experiment_id), round_id)
    assert report["summary"]["완료 답안 수"] == 5 and report["summary"]["평균 평가 점수"] == 0
    assert report["summary"]["평균 점수 차이"] == -sum(maximum - 2 for maximum in SCORE_LIMITS[slot].values())
    assert all(r["최저"] == r["최고"] == 0 and r["반복 표준편차"] is None and r["점수 폭"] is None for r in report["answers"])
    assert [r["배점"] for r in report["criteria"]] == list(SCORE_LIMITS[slot].values())
    assert len(report["criteria"]) == (2 if slot < 53 else 3)
    charts = []
    original_chart = st.vega_lite_chart
    def chart(data=None, spec=None, **kwargs):
        charts.append((data, spec))
        return original_chart(data, spec=spec, **kwargs)
    monkeypatch.setattr(st, "vega_lite_chart", chart)
    app = AppTest.from_file(MANAGER_APP, default_timeout=30).run()
    app.selectbox(key="writing-manager-selected").set_value(experiment_id).run()
    assert not app.exception
    assert next(m.value for m in app.metric if m.label == "평균 평가 점수") == "0.00점"
    scatter_spec = next(spec for _, spec in charts if spec["title"] == "사전 예상과 평가 평균")
    assert scatter_spec["encoding"]["y"]["scale"]["domain"] == [0, sum(SCORE_LIMITS[slot].values())]
    criteria_data = next(data for data, spec in charts if spec["title"] == "영역별 평균 점수")
    assert {r["영역"] for r in criteria_data} == {writing_evaluation.SCORE_LABELS[key] for key in SCORE_LIMITS[slot]}


def test_results_ui_charts_match_completed_answers_and_show_full_evaluation(tmp_path, fake_models, monkeypatch):
    import copy
    import csv
    import io
    import altair as alt
    import streamlit as st

    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    experiment_id, round_id = mixed_result_round(storage, monkeypatch)
    calls_before = len(fake_models)
    charts, downloads = [], {}
    original_chart, original_download = st.vega_lite_chart, st.download_button
    def chart(data=None, spec=None, **kwargs):
        charts.append((copy.deepcopy(data), copy.deepcopy(spec)))
        alt.Chart.from_dict({**copy.deepcopy(spec), "data": {"values": copy.deepcopy(data)}})
        return original_chart(data, spec=spec, **kwargs)
    def download(label, data, *args, **kwargs):
        downloads[label] = data
        return original_download(label, data, *args, **kwargs)
    monkeypatch.setattr(st, "vega_lite_chart", chart)
    monkeypatch.setattr(st, "download_button", download)
    app = AppTest.from_file(MANAGER_APP, default_timeout=30).run()
    app.selectbox(key="writing-manager-selected").set_value(experiment_id).run()
    assert not app.exception and app.radio(key=f"manager-section-{experiment_id}").value == "실험 결과"
    charts.clear()
    app.selectbox(key=f"manager-result-case-{experiment_id}").set_value("A001").run()
    assert not app.exception and len(charts) == 5
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["평균 평가 점수"] == "24.00점" and metrics["평균 점수 차이"] == "+0.00점"
    assert metrics["평균 절대 차이"] == "5.33점" and metrics["완료 답안 수"] == "2 / 5"
    scatter = next(data for data, spec in charts if spec["title"] == "사전 예상과 평가 평균")
    assert {r["답안"] for r in scatter} == {"A001", "A002"}
    table = next(frame.value for frame in app.dataframe if "요약 포함" in frame.value.columns)
    assert set(table.loc[table["요약 포함"], "답안"]) == {r["답안"] for r in scatter}
    assert sum(r["평가 평균"] for r in scatter) / len(scatter) == 24
    level_data = next(data for data, spec in charts if spec["title"] == "수준별 평균 점수")
    assert len(level_data) == 4 and all(r["답안 수"] == 1 for r in level_data)
    criteria_data = next(data for data, spec in charts if spec["title"] == "영역별 평균 점수")
    assert len(criteria_data) == 6 and all(r["답안 수"] == 2 for r in criteria_data)
    repetitions = next(data for data, spec in charts if spec["title"] == "반복별 평가 점수")
    assert [r["반복"] for r in repetitions] == [1, 2, 3]
    assert sorted(r["평가 점수"] for r in repetitions) == [29, 29, 30]
    assert any("상세 평가 종합 의견" in text.value for text in app.markdown)
    assert any("모의 검토 사유" in warning.value for warning in app.warning)
    assert any("모의 불확실성" in text.value for text in app.markdown)
    frames = [frame.value for frame in app.dataframe]
    assert any("원문 인용" in frame.columns and "판단 근거" in frame.columns for frame in frames)
    assert any("과제" in frame.columns and "수행" in frame.columns for frame in frames)
    assert any("수정 제안" in frame.columns and frame.iloc[0]["이유"] == "모의 수정 제안 근거" for frame in frames)
    summary_csv = list(csv.DictReader(io.StringIO(downloads["실험 결과 요약 CSV 받기"].decode("utf-8-sig"))))
    assert float(summary_csv[0]["평균 절대 차이"]) == 5.33
    assert len(list(csv.DictReader(io.StringIO(downloads["전체 답안 결과 CSV 받기"].decode("utf-8-sig"))))) == 5
    app.selectbox(key=f"manager-result-case-{experiment_id}").set_value("A003").run()
    assert not app.exception
    failed_run = next(t["run_id"] for t in lab.load_experiment(storage, experiment_id)["rounds"][0]["tasks"]
                      if t["case_id"] == "A003" and t["status"] == "failed")
    app.selectbox(key=f"manager-result-run-{experiment_id}-{round_id}-A003").set_value(failed_run).run()
    assert not app.exception and any("의도한 모의 실패" in error.value for error in app.error)
    assert len(fake_models) == calls_before


def test_results_ui_round_switch_uses_own_completion_and_handles_no_result(tmp_path, fake_models):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    experiment_id = create(storage)
    lab.run_pending(storage, experiment_id, "generation")
    lab.run_pending(storage, experiment_id, "prior")
    finished = add_round(storage, experiment_id, repeats=1)
    lab.run_pending(storage, experiment_id, "evaluation", finished)
    pending = add_round(storage, experiment_id)
    app = AppTest.from_file(MANAGER_APP, default_timeout=30).run()
    app.selectbox(key="writing-manager-selected").set_value(experiment_id).run()
    assert not app.exception and app.selectbox(key=f"manager-result-round-{experiment_id}").value == pending
    assert next(m.value for m in app.metric if m.label == "평균 평가 점수") == "—"
    assert any("반복 평가를 모두 마친 답안이 없습니다" in info.value for info in app.info)
    app.selectbox(key=f"manager-result-round-{experiment_id}").set_value(finished).run()
    assert not app.exception
    assert next(m.value for m in app.metric if m.label == "평균 평가 점수") == "30.00점"
    assert next(m.value for m in app.metric if m.label == "완료 답안 수") == "5 / 5"
