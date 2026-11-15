"""A local workshop for source-backed prompts and human scoring comparisons."""
import json
from collections import defaultdict

import streamlit as st
from pydantic import ValidationError

from topik_question_lab.common.generation.providers import can_gateway_call, manual_result
from topik_question_lab.common.writing_evaluation import (
    OPERATION, SCORE_LABELS, SCORE_LIMITS, EvaluationRequest, LearnerAnswer,
    build_evaluation_request, character_counts, comparison_rows, evaluate_writing,
    human_review, save_evaluation, save_human_review, validate_evaluation,
)
from topik_question_lab.common.models import ProviderResult
from topik_question_lab.menus.writing.writing_evaluation_prompts import (
    evaluation_settings, load_answer_references, rubric_for, save_evaluation_settings,
)
from topik_question_lab.menus.writing.writing_pipeline import PDF_ROOT
from topik_question_lab.menus.writing.writing_prompt_ui import render_provider
from topik_question_lab.menus.writing.writing_ui import render_question


def render_sources(reference, answer_reference, rubric, pdf_renderer):
    with st.expander("기출 답안과 채점 기준"):
        st.caption(f"답안 출처: {answer_reference.source_file} · {answer_reference.source_page}쪽. "
                   "배점은 문항의 최대 점수이며, 모범답안의 실제 채점 기록이 아닙니다.")
        if answer_reference.blank_answers:
            for key, values in answer_reference.blank_answers.items():
                st.write(f"{SCORE_LABELS[key]}: " + " / ".join(values))
        else:
            st.text(answer_reference.text)
        if answer_reference.source_char_count:
            st.caption(f"PDF에 기재된 분량: {answer_reference.source_char_count}자. 전사 텍스트의 계산값과 다를 수 있습니다.")
        if answer_reference.published_rules:
            st.write("이 문항의 출처에 명시된 채점 기준")
            for rule in answer_reference.published_rules:
                st.write("• " + rule)
        else:
            st.info("이 답안 PDF에는 문항별 세부 부분점수 기준이 없습니다. 공통 기준을 적용하고 세부 점수는 실험적 추정으로 기록합니다.")
        st.write("공통 평가 기준 · 2017 안내서")
        st.caption("출처: " + rubric["source_file"] + " · " + ", ".join(map(str, rubric["source_pages"])) + "쪽")
        criteria = rubric["published_criteria"]
        if isinstance(criteria, dict):
            st.dataframe([{"영역": SCORE_LABELS[key], "배점": maximum, "기준": " / ".join(criteria[key])}
                          for key, maximum in rubric["max_scores"].items()], hide_index=True, width="stretch")
            bands = rubric["published_bands"]
            st.dataframe([{"수준": band, "내용·구성 각각": f"{bounds[0]}~{bounds[1]}",
                           "언어 사용(2배 적용)": "~".join(str(value * 2) for value in bands["language_raw"][band])}
                          for band, bounds in bands["content_and_organization"].items()], hide_index=True, width="stretch")
            for rule in rubric["published_guidance"]:
                st.write("• " + rule)
        else:
            for rule in criteria:
                st.write("• " + rule)
        for note in rubric["application_notes"]:
            st.caption("적용 시 유의: " + note)
        for label, filename in (("답안 원본 PDF", answer_reference.source_file), ("공통 기준 PDF", rubric["source_file"])):
            path = PDF_ROOT / filename
            if path.is_file():
                st.download_button(label, path.read_bytes(), filename, "application/pdf", key=f"evaluation-pdf-{label}")
        if pdf_renderer and st.checkbox("답안 원본 페이지 보기", key=f"evaluation-source-{reference.source_key}"):
            path = PDF_ROOT / answer_reference.source_file
            try:
                st.image(pdf_renderer(str(path), path.stat().st_mtime_ns, answer_reference.source_page))
            except (OSError, RuntimeError):
                st.warning("원본 미리보기를 열 수 없습니다. PDF를 내려받아 확인하세요.")


def render_saved_evaluation(storage, run):
    result = ProviderResult.model_validate_json(run["result_json"])
    payload = json.loads(run["prompt_user"])
    request = EvaluationRequest(run["prompt_system"], run["prompt_user"], payload["request_id"])
    st.caption(f"실행 #{run['id']} · {payload['source_key']} · {run['created_at']} · {run['model']}")
    with st.expander("이 실행의 원본 답안·프롬프트·응답"):
        for key, value in payload["answer"].items():
            st.text(SCORE_LABELS.get(key, "서술형 답안"))
            st.code(value, language=None)
        st.code(run["prompt_system"], language=None)
        st.code(run["prompt_user"], language="json")
        st.code(result.raw_response, language="json")
    if result.error:
        st.error("평가 결과를 확정하지 않았습니다: " + result.error)
        return
    try:
        evaluation = validate_evaluation(result, request)
    except (ValueError, ValidationError):
        st.error("저장된 평가 기록을 현재 형식으로 검증할 수 없습니다.")
        return
    limits = SCORE_LIMITS[evaluation.type_slot]
    st.metric("학습용 예상 점수", f"{evaluation.total} / {sum(limits.values())}")
    st.write(evaluation.summary)
    for reason in evaluation.review_reasons:
        st.warning(reason)
    st.dataframe([
        {"영역": SCORE_LABELS[key], "점수": item.score, "최대": limits[key], "판단 근거": item.rationale,
         "원문 인용": " / ".join(item.quotes), "내용·형식 점수": str(item.parts) if item.parts else ""}
        for key, item in evaluation.scores.items()
    ], hide_index=True, width="stretch")
    st.dataframe([
        {"과제": payload["tasks"][item.requirement_id], "수행": item.status,
         "근거": item.reason, "원문 인용": " / ".join(item.quotes)} for item in evaluation.task_checks
    ], hide_index=True, width="stretch")
    if evaluation.corrections:
        st.dataframe([{"원문": item.original, "수정 제안": item.suggestion, "이유": item.reason}
                      for item in evaluation.corrections], hide_index=True, width="stretch")

    review = human_review(storage, run["id"])
    with st.expander("사람 채점 입력·비교", expanded=bool(review)):
        st.caption("이 실행에 사용된 원본 답안을 직접 채점하세요. 사람 점수는 모델에 전달되지 않습니다.")
        with st.form(f"evaluation-human-{run['id']}"):
            human_scores = {}
            columns = st.columns(len(limits))
            for column, (key, maximum) in zip(columns, limits.items()):
                human_scores[key] = column.number_input(
                    SCORE_LABELS[key], min_value=0, max_value=maximum,
                    value=review.get("scores", {}).get(key, 0), step=2 if key == "language" else 1,
                    key=f"evaluation-human-score-{run['id']}-{key}")
            notes = st.text_area("사람 채점 근거", review.get("notes", ""), key=f"evaluation-human-notes-{run['id']}")
            if st.form_submit_button("사람 채점 저장"):
                try:
                    save_human_review(storage, run["id"], human_scores, notes)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    review = human_review(storage, run["id"])
                    st.success("사람 채점을 저장했습니다.")
        if review:
            st.dataframe([{"영역": SCORE_LABELS[key], "AI": evaluation.scores[key].score, "사람": score,
                           "차이(AI−사람)": evaluation.scores[key].score - score}
                          for key, score in review["scores"].items()], hide_index=True, width="stretch")
    st.download_button("실험 기록 JSON 받기", json.dumps({
        "schema_version": 1, "run_id": run["id"], "system": run["prompt_system"],
        "request": payload, "result": result.model_dump(), "human_review": review,
    }, ensure_ascii=False, indent=2), f"writing_evaluation_{run['id']}.json", "application/json",
        key=f"evaluation-export-{run['id']}")


def render_comparisons(storage, runs):
    with st.expander("프롬프트·모델별 실험 비교"):
        rows = comparison_rows(storage, runs)
        if not rows:
            st.info("검증을 통과한 평가 결과가 아직 없습니다.")
            return
        st.dataframe(rows, hide_index=True, width="stretch")
        errors = defaultdict(list)
        repeats = defaultdict(list)
        for row in rows:
            group = (row["서비스"], row["모델"], row["프롬프트"])
            if row["사람 점수"] is not None:
                errors[group].append(abs(row["차이(AI−사람)"]))
            repeats[(*group, row["답안"])].append(row["AI 점수"])
        if errors:
            st.dataframe([{"서비스": provider, "모델": model, "프롬프트": prompt, "사람 채점 실행 수": len(values),
                           "평균 절대 점수 차이": round(sum(values) / len(values), 2)}
                          for (provider, model, prompt), values in errors.items()], hide_index=True, width="stretch")
        repeated = [{"서비스": provider, "모델": model, "프롬프트": prompt, "답안": answer, "반복 수": len(values),
                     "최저": min(values), "최고": max(values), "점수 폭": max(values) - min(values)}
                    for (provider, model, prompt, answer), values in repeats.items() if len(values) > 1]
        if repeated:
            st.dataframe(repeated, hide_index=True, width="stretch")
        st.caption("비교 단위는 실행 기록입니다. 같은 답안·프롬프트·모델로 반복 실행하면 점수 변동을 비교할 수 있습니다. "
                   "사람 채점과의 차이는 등록한 사례에서만 측정한 값입니다.")


def render_evaluation_lab(storage, references, slot, *, pdf_renderer=None):
    st.subheader(f"{slot}번 평가 프롬프트 실험실")
    st.caption("기출·답안 확인 → 익명 답안 입력 → 평가 프롬프트 조정 → 평가 → 사람 채점 비교")
    st.caption("이름·연락처·학생 식별정보를 제외한 연습 답안만 입력하세요. 실행 시 선택한 모델로 전송하고 로컬 실험 기록에 저장합니다.")
    answers = load_answer_references(references)
    candidates = {r.source_key: r for r in sorted(references, key=lambda r: -r.source_exam) if r.question.type_slot == slot}
    source_key = st.selectbox("평가할 기출", list(candidates), format_func=lambda value: f"{value.split(':')[0]}회 {slot}번",
                              key=f"evaluation-reference-{slot}")
    reference, answer_reference = candidates[source_key], answers[source_key]
    rubric = rubric_for(slot)
    render_question(reference.question, instruction=reference.source_instruction)
    render_sources(reference, answer_reference, rubric, pdf_renderer)

    st.write("평가할 원본 답안")
    if st.button("모범답안을 입력란에 불러오기", key=f"evaluation-load-answer-{source_key}"):
        if slot < 53:
            for key in ("a", "b"):
                st.session_state[f"evaluation-answer-{source_key}-{key}"] = answer_reference.blank_answers[key][0]
        else:
            st.session_state[f"evaluation-answer-{source_key}-text"] = answer_reference.text
    if slot < 53:
        answer = LearnerAnswer(**{
            key: st.text_area(f"{SCORE_LABELS[key]} 답안", key=f"evaluation-answer-{source_key}-{key}", max_chars=2000)
            for key in ("a", "b")})
    else:
        text = st.text_area("서술형 답안", height=240, key=f"evaluation-answer-{source_key}-text", max_chars=20000)
        answer = LearnerAnswer(text=text)
        counts = character_counts(text)
        st.caption(f"공백 포함·줄바꿈 제외 {counts['공백_포함_줄바꿈_제외']}자 / 공백 제외 {counts['모든_공백_제외']}자. "
                   "원고지 칸 수와 다를 수 있으며 분량에 따른 자동 감점은 하지 않습니다.")

    settings = evaluation_settings(storage, slot)
    with st.expander("평가 프롬프트 편집"):
        st.caption("저장한 설정만 평가에 적용합니다. 기존 문제 생성 프롬프트와 별도로 저장합니다.")
        with st.form(f"evaluation-prompt-form-{slot}"):
            system = st.text_area("평가 시스템 프롬프트", settings["system"], height=360, key=f"evaluation-system-{slot}")
            notes = st.text_area("실험용 보조 지침", settings["notes"], height=140, key=f"evaluation-notes-{slot}",
                                 help="출처 기준과 구분해 적용됩니다. 사람 채점 점수는 여기에 넣지 마세요.")
            if st.form_submit_button("평가 프롬프트 저장"):
                try:
                    save_evaluation_settings(storage, slot, system, notes)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    settings = evaluation_settings(storage, slot)
                    st.success("평가 프롬프트를 저장했습니다.")
    request = build_evaluation_request(reference, answer_reference, answer, system=settings["system"],
                                       rubric=rubric, notes=settings["notes"])
    with st.expander("이번 평가 요청 확인·내보내기"):
        st.code(request.system, language=None)
        st.code(request.user, language="json")
        st.download_button("평가 요청 JSON 받기", json.dumps({"system": request.system, "user": request.payload},
                            ensure_ascii=False, indent=2), "writing_evaluation_request.json", "application/json")
    provider, model, capabilities = render_provider(storage, prefix="writing-evaluation", operation="평가", use_saved_default=True)
    blank = not any(value.strip() for value in answer.for_slot(slot).values())
    if blank:
        st.info("빈 답안을 평가하면 모델 호출 없이 미응답 0점으로 기록합니다.")
    outcome = None
    if st.button("답안 평가 실행", type="primary", key="writing-evaluation-run",
                 disabled=not blank and (not model or not can_gateway_call(provider))):
        with st.spinner("원본 답안을 평가하고 결과를 검증하는 중…"):
            result = evaluate_writing(request, provider=provider, model=model, model_capabilities=capabilities)
            outcome = save_evaluation(storage, result, request)
    with st.expander("외부 모델 평가 응답 붙여넣기"):
        st.caption("위 요청의 request_id를 포함한 JSON 응답을 붙여넣으세요. 답안이나 프롬프트가 바뀌면 다시 요청해야 합니다.")
        raw = st.text_area("평가 응답 JSON", height=200, key=f"evaluation-manual-{source_key}")
        if st.button("평가 응답 검사·저장", disabled=not raw.strip(), key="writing-evaluation-import"):
            outcome = save_evaluation(storage, manual_result(provider, model or "manual", OPERATION, raw), request)
    history_key = f"evaluation-history-{slot}"
    if outcome:
        st.session_state[history_key] = outcome["run_id"]
        if outcome["error"]:
            st.error("평가 결과를 확정하지 않았습니다: " + outcome["error"])
        else:
            st.success(f"평가 기록 #{outcome['run_id']} 저장 완료")

    runs = [r for r in storage.list_runs(OPERATION) if r["question_type"] == f"writing_pbt_{slot}"]
    if not runs:
        st.info("아직 저장된 평가 실행이 없습니다.")
        return
    st.subheader("저장된 평가 결과")
    by_id = {r["id"]: r for r in runs}
    run_id = st.selectbox("평가 실행 기록", list(by_id), key=history_key,
                          format_func=lambda value: f"#{value} · {json.loads(by_id[value]['prompt_user'])['source_key']} · {by_id[value]['model']}")
    render_saved_evaluation(storage, by_id[run_id])
    render_comparisons(storage, runs)
