"""Foreground bulk experiments and side-by-side provisional/actual score comparisons."""
import json
import time
from collections import Counter
from statistics import mean

import streamlit as st

from topik_question_lab.common.generation.providers import can_gateway_call
from topik_question_lab.common.writing_evaluation import LearnerAnswer, SCORE_LABELS, EvaluationResult
from topik_question_lab.common import writing_experiments as lab
from topik_question_lab.menus.writing.writing_evaluation_prompts import (
    evaluation_settings, load_answer_references, rubric_for,
)
from topik_question_lab.menus.writing.writing_evaluation_ui import render_saved_evaluation
from topik_question_lab.menus.writing.writing_prompt_ui import render_provider
from topik_question_lab.menus.writing.writing_ui import render_question


def _model(storage, prefix, label):
    provider, model, capabilities = render_provider(storage, prefix=prefix, operation=label, use_saved_default=True)
    return {"provider": provider, "model": model, "capabilities": capabilities}


def _execute(storage, experiment, stage, round_id=None, *, retry=False):
    try:
        if retry:
            lab.retry_failed(storage, experiment["id"], stage, round_id)
        progress = st.progress(0, text="실행 준비 중")
        with st.spinner("이 화면에서 한 항목씩 실행합니다. 완료된 응답은 즉시 저장합니다."):
            lab.run_pending(storage, experiment["id"], stage, round_id,
                            on_progress=lambda done, total: progress.progress(done / total, text=f"{done} / {total} 처리"))
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.rerun()


def _stage_controls(storage, experiment, stage, label, model, round_id=None, ready=True):
    items = lab.task_items(experiment, stage, round_id)
    counts = Counter(task["status"] for _, task in items)
    st.caption(f"완료 {counts['succeeded']} · 대기 {counts['pending']} · 실패 {counts['failed']} · 실행 중 {counts['running']}")
    blocked = not ready or any(t["status"] == "running" for t in lab.all_tasks(experiment))
    if lab.legacy_scoring_stage(experiment, stage, round_id):
        blocked = True
        st.info("참고답안을 포함한 기존 평가·사전 판단 기록입니다. 기존 차수의 실행·재시도는 중지됩니다. "
                "실제 평가는 같은 답안으로 새 평가 차수를 만들어 비교할 수 있습니다.")
    enabled = bool(model["model"]) and can_gateway_call(model["provider"])
    if not enabled:
        st.info("실험에 저장된 모델의 API 연결 설정이 필요합니다.")
    col1, col2 = st.columns(2)
    key = f"experiment-{experiment['id']}-{stage}-{round_id}"
    if col1.button(f"{label} 시작·이어서", key=key + "-run", type="primary",
                   disabled=blocked or not enabled or not counts["pending"]):
        _execute(storage, experiment, stage, round_id)
    if col2.button(f"실패한 {label} 재시도", key=key + "-retry",
                   disabled=blocked or not enabled or not counts["failed"]):
        _execute(storage, experiment, stage, round_id, retry=True)
    failed = [{"답안": case["id"], "반복": task.get("repeat", 1), "오류": task["error"]}
              for case, task in items if task["status"] == "failed"]
    if failed:
        st.dataframe(failed, hide_index=True, width="stretch")


def _case_editor(storage, experiment):
    cases = {case["id"]: case for case in experiment["cases"]}
    selected = st.selectbox("확인할 합성 답안", list(cases),
        format_func=lambda key: f"{key} · 목표 {cases[key]['goal_level']} · {cases[key]['profile']}",
        key=f"experiment-case-{experiment['id']}")
    case = cases[selected]
    st.caption("생성 목표는 설계 의도입니다. 실제 완성도는 별도 사전 판단에서 확인합니다.")
    if case["design_notes"]:
        st.write("생성 의도: " + case["design_notes"])
    locked = bool(experiment["rounds"]) or any(c["prior"]["status"] != "pending" for c in cases.values())
    locked = locked or any(t["status"] == "running" for t in lab.all_tasks(experiment))
    if locked:
        st.caption("사전 판단을 시작한 답안 묶음은 고정됩니다. 이 묶음으로 평가 프롬프트를 바꿔 비교할 수 있습니다.")
    answer = case["answer"] or {"a": "", "b": "", "text": ""}
    slot = experiment["reference"]["question"]["type_slot"]
    with st.form(f"experiment-edit-{experiment['id']}-{selected}"):
        values = {key: st.text_area(SCORE_LABELS.get(key, "답안"), answer[key],
                    height=140 if slot < 53 else 240, disabled=locked,
                    max_chars=2000 if slot < 53 else 20000)
                  for key in (("a", "b") if slot < 53 else ("text",))}
        if st.form_submit_button("답안 수정 저장", disabled=locked):
            try:
                lab.edit_answer(storage, experiment["id"], selected, LearnerAnswer(**values))
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()


def _compare(storage, experiment, round_data):
    runs = lab.experiment_runs(storage, experiment["id"])
    rows = lab.comparison_rows(experiment, runs, round_data["id"])
    st.subheader("사전 예상과 실제 평가 비교")
    st.caption("사전 예상은 AI의 잠정 판단입니다. 차이가 큰 답안은 검토 대상이며, 차이만으로 오채점이라고 판단하지 않습니다.")
    ordered = sorted(rows, key=lambda row: abs(row["차이(실제−예상)"]) if row["차이(실제−예상)"] is not None else -1, reverse=True)
    st.dataframe(ordered, hide_index=True, width="stretch")
    completed = [row for row in rows if row["완료"] == row["예정"]]
    st.caption(f"반복 평가를 모두 완료한 답안 {len(completed)} / {len(rows)}개. 아래 요약은 완료된 답안만 사용합니다.")
    if completed:
        st.metric("사전 예상과 평균 절대 점수 차이", f"{mean(abs(row['차이(실제−예상)']) for row in completed):.2f}점")
        st.scatter_chart(completed, x="사전 예상", y="평가 평균", color="생성 목표")
        distributions = []
        for level in lab.LEVELS:
            group = [row for row in completed if row["생성 목표"] == level]
            if group:
                distributions.append({"생성 목표": level, "답안 수": len(group),
                    "평균 사전 예상": round(mean(row["사전 예상"] for row in group), 2),
                    "평균 실제 평가": round(mean(row["평가 평균"] for row in group), 2)})
        st.dataframe(distributions, hide_index=True, width="stretch")

    case_id = st.selectbox("자세히 비교할 답안", [row["답안"] for row in ordered], key=f"experiment-detail-{round_data['id']}")
    case = next(c for c in experiment["cases"] if c["id"] == case_id)
    original = LearnerAnswer.model_validate(case["answer"]).for_slot(experiment["reference"]["question"]["type_slot"])
    st.write("원본 답안")
    for key, value in original.items():
        st.text(SCORE_LABELS.get(key, "서술형"))
        st.code(value, language=None)
    assessment = case["assessment"]
    left, right = st.columns(2)
    with left:
        st.write("AI 사전 판단")
        st.metric(assessment["level"], f"{sum(assessment['scores'].values())}점 (잠정)")
        st.dataframe([{"영역": SCORE_LABELS[key], "예상": score, "근거": assessment["reasons"][key]}
                      for key, score in assessment["scores"].items()], hide_index=True, width="stretch")
        for reason in assessment["uncertainties"]:
            st.write(reason)
    selected_tasks = [t for t in round_data["tasks"] if t["case_id"] == case_id and t["status"] == "succeeded"]
    with right:
        st.write("실제 평가 프롬프트 결과")
        if selected_tasks:
            scored = [EvaluationResult.model_validate(json.loads(runs[t["run_id"]]["result_json"])["parsed_json"]) for t in selected_tasks]
            st.dataframe([{"영역": SCORE_LABELS[key], "평균": round(mean(s.scores[key].score for s in scored), 2),
                           "예상과 차이": round(mean(s.scores[key].score for s in scored) - score, 2)}
                          for key, score in assessment["scores"].items()], hide_index=True, width="stretch")
        else:
            st.info("완료된 평가가 없습니다.")
    if selected_tasks:
        by_run = {t["run_id"]: t for t in selected_tasks}
        run_id = st.selectbox("평가 근거를 볼 반복 실행", list(by_run),
            format_func=lambda value: f"{by_run[value]['repeat']}회 · 실행 #{value}", key=f"experiment-run-{round_data['id']}-{case_id}")
        with st.expander("실제 평가의 근거·수정 제안·사람 채점"):
            render_saved_evaluation(storage, runs[run_id])
    with st.expander("평가 차수별 비교"):
        comparisons = []
        for other in experiment["rounds"]:
            for row in lab.comparison_rows(experiment, runs, other["id"]):
                comparisons.append({"평가 차수": other["name"], "서비스": other["evaluator"]["provider"],
                    "차수 ID": other["id"][:8], "모델": other["evaluator"]["model"],
                    "프롬프트": lab.digest({"system": other["system"], "notes": other["notes"], "rubric": other["rubric"]})[:12], **row})
        st.dataframe(comparisons, hide_index=True, width="stretch")
    st.download_button("전체 실험 기록 JSON 받기", json.dumps({"experiment": experiment, "runs": list(runs.values())},
                        ensure_ascii=False, indent=2), f"writing_experiment_{experiment['id']}.json", "application/json")


def render_bulk_experiments(storage, references, slot):
    st.subheader("대량 실험")
    st.caption("합성 답안 생성 → 독립적인 사전 판단 → 실제 평가 프롬프트 실행 → 점수 비교")
    st.info("새 사전 판단·실제 평가는 문제·학생 답안·채점 기준만 사용합니다. 참고답안은 평가 모델에 전달하지 않습니다.")
    st.caption("합성 답안 생성 단계는 기존처럼 기출 참고답안을 사용합니다.")
    st.caption("화면에서 순차 실행합니다. 화면 이동·앱 종료 시 중단될 수 있으며 완료 기록은 보존됩니다. "
               "모델 호출은 항목당 한 번이며 실패 항목은 직접 재시도합니다.")
    candidates = {r.source_key: r for r in sorted(references, key=lambda r: -r.source_exam) if r.question.type_slot == slot}
    source_key = st.selectbox("실험할 기출 문항", list(candidates),
        format_func=lambda key: f"{key.split(':')[0]}회 {slot}번", key=f"experiment-source-{slot}")
    reference = candidates[source_key]
    with st.expander("선택한 문항 확인"):
        render_question(reference.question, instruction=reference.source_instruction)
    selection_key = f"experiment-selected-{source_key}"
    with st.expander("새 답안 묶음 만들기", expanded=not lab.list_experiments(storage, source_key)):
        name = st.text_input("실험 이름", f"{source_key} 수준별 답안 실험", key=f"experiment-name-{source_key}")
        per_level = st.number_input("수준별 답안 수", 1, 20, 5, key="experiment-per-level")
        total = per_level * 5
        st.write(f"5개 수준 × {per_level}개 = 총 {total}개 답안")
        st.caption(f"답안 생성 {total}회 + 사전 판단 {total}회. 실제 평가를 3회 반복하면 {total * 3}회가 추가되어 총 {total * 5}회입니다. 실패 후 재시도는 별도입니다.")
        st.write("답안 생성 모델")
        generator = _model(storage, "experiment-generator", "답안 생성")
        st.write("사전 판단 모델")
        prior = _model(storage, "experiment-prior", "사전 판단")
        st.caption("같은 모델도 선택할 수 있지만 호출 맥락은 분리됩니다. 생성 목표와 의도는 사전 판단·실제 평가에 전달하지 않습니다.")
        if st.button("실험 만들기", key="experiment-create", disabled=not name.strip() or not generator["model"] or not prior["model"]):
            experiment_id = lab.create_experiment(storage, reference, load_answer_references(references)[source_key], rubric_for(slot),
                name=name, per_level=per_level, generator=lab.ModelChoice(**generator), prior=lab.ModelChoice(**prior))
            st.session_state[selection_key] = experiment_id
            st.rerun()
    experiments = lab.list_experiments(storage, source_key)
    if not experiments:
        st.info("실험을 만든 뒤 답안을 생성할 수 있습니다. 실험 만들기 자체는 모델을 호출하지 않습니다.")
        return
    by_id = {e["id"]: e for e in experiments}
    experiment_id = st.selectbox("저장된 실험", list(by_id), format_func=lambda key: f"{by_id[key]['name']} · {by_id[key]['created_at']}", key=selection_key)
    experiment = by_id[experiment_id]
    if lab.legacy_scoring_stage(experiment, "prior"):
        st.caption("이 실험의 기존 사전 예상 점수는 참고답안을 제공한 조건에서 산출되었습니다. "
                   "새 평가 차수의 점수와 입력 조건이 다릅니다.")
    running = [t for t in lab.all_tasks(experiment) if t["status"] == "running"]
    if running:
        st.info("응답 대기 중인 호출이 있습니다. 다른 화면에서 실행 중인지 확인하세요. "
                "실행이 중단된 경우 10분이 지난 호출을 실패 상태로 전환해 직접 재시도할 수 있습니다.")
        if st.button("중단된 호출 상태 복구", key="experiment-recover", disabled=not any(time.time() - t["started_at"] >= lab.LEASE_SECONDS for t in running)):
            lab.recover_interrupted(storage, experiment_id)
            st.rerun()
        if st.button("실행 상태 새로고침", key="experiment-refresh"):
            st.rerun()
    st.write("1. 다양한 수준의 답안 생성")
    _stage_controls(storage, experiment, "generation", "답안 생성", experiment["generator"])
    with st.expander("답안 확인·수정"):
        _case_editor(storage, experiment)
    st.write("2. 독립적인 사전 예상 판단")
    answers_ready = all(case["answer"] is not None for case in experiment["cases"])
    if not answers_ready:
        st.caption("모든 답안이 준비되면 사전 판단을 시작할 수 있습니다.")
    _stage_controls(storage, experiment, "prior", "사전 판단", experiment["prior_model"], ready=answers_ready)
    assessments = [{"답안": c["id"], "목표": c["goal_level"], "사전 판단": c["assessment"]["level"],
                    "예상 점수": sum(c["assessment"]["scores"].values())}
                   for c in experiment["cases"] if c.get("assessment")]
    if assessments:
        st.dataframe(assessments, hide_index=True, width="stretch")
    st.write("3. 실제 평가 프롬프트로 반복 평가")
    prior_ready = all(c["prior"]["status"] == "succeeded" for c in experiment["cases"])
    round_key = f"experiment-round-{experiment_id}"
    with st.expander("평가 차수 추가 · 동일한 답안으로 프롬프트 비교", expanded=not experiment["rounds"]):
        settings = evaluation_settings(storage, slot)
        label = st.text_input("평가 차수 이름", f"평가 {len(experiment['rounds']) + 1}", key=f"experiment-round-label-{experiment_id}-{len(experiment['rounds'])}")
        repeats = st.number_input("답안별 평가 반복 횟수", 1, 5, 3, key=f"experiment-repeats-{experiment_id}")
        st.caption(f"실제 평가 예정: {len(experiment['cases'])}개 × {repeats}회 = {len(experiment['cases']) * repeats}회 호출")
        evaluator = _model(storage, "experiment-evaluator", "실제 평가")
        system = st.text_area("이 차수의 평가 시스템 프롬프트", settings["system"], height=220, key=f"experiment-system-{experiment_id}")
        notes = st.text_area("이 차수의 보조 지침", settings["notes"], key=f"experiment-notes-{experiment_id}")
        st.caption("참고답안 없는 개별 평가의 저장된 프롬프트를 초기값으로 사용합니다. "
                   "수정 내용은 새 차수에만 적용하고 참고답안·생성 목표·예상 점수는 넣지 마세요.")
        if not prior_ready:
            st.caption("모든 사전 판단이 완료되면 실제 평가 차수를 만들 수 있습니다.")
        if st.button("평가 차수 만들기", key="experiment-add-round",
                     disabled=not prior_ready or bool(running) or not system.strip() or not evaluator["model"] or not label.strip()):
            try:
                st.session_state[round_key] = lab.create_round(storage, experiment_id, evaluator=lab.ModelChoice(**evaluator),
                    system=system, notes=notes, rubric=rubric_for(slot), repeats=repeats, name=label)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()
    if experiment["rounds"]:
        rounds = {r["id"]: r for r in experiment["rounds"]}
        round_id = st.selectbox("비교할 평가 차수", list(rounds), key=round_key,
            format_func=lambda key: f"{rounds[key]['name']} · {rounds[key]['evaluator']['model']} · {rounds[key]['repeats']}회 반복")
        round_data = rounds[round_id]
        st.caption(f"평가 차수 생성: {round_data['created_at']} · ID {round_id[:8]}")
        _stage_controls(storage, experiment, "evaluation", "실제 평가", round_data["evaluator"], round_id)
        _compare(storage, experiment, round_data)
