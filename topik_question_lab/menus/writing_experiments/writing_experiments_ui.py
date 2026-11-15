"""List and inspect experiments; execution stays in the existing workshop."""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from statistics import mean

import streamlit as st

from topik_question_lab.common import writing_experiments as lab
from topik_question_lab.common.ui.navigation import menu_page
from topik_question_lab.common.writing_evaluation import EvaluationResult, SCORE_LABELS, SCORE_LIMITS, human_review
from topik_question_lab.common.writing_models import WritingQuestion
from topik_question_lab.menus.writing.writing_evaluation_ui import render_saved_evaluation
from topik_question_lab.menus.writing.writing_ui import render_question


KST = timezone(timedelta(hours=9))
STAGES = {"generation": "답안 생성", "prior": "사전 판단", "evaluation": "실제 평가"}
STATUSES = {"pending": "대기", "running": "응답 대기", "failed": "실패", "succeeded": "완료"}
PAGE_SIZE = 50


def korean_time(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(KST)


def csv_bytes(rows):
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            # Spreadsheet apps must treat model/user text as data, never formulas.
            writer.writerow({key: "'" + value if isinstance(value, str)
                and value.lstrip().startswith(("=", "+", "-", "@")) else value for key, value in row.items()})
    return output.getvalue().encode("utf-8-sig")


def model_names(item):
    return {f"{choice['provider']} · {choice['model']}"
            for choice in [item["generator"], item["prior_model"], *item["evaluators"]]}


def filter_summaries(items, *, query="", slot=None, exam=None, status="전체", model=None,
                     archive="보관 제외", dates=()):
    selected = []
    for item in items:
        if archive == "보관 제외" and item["archived"] or archive == "보관만" and not item["archived"]:
            continue
        if slot and item["slot"] != slot or exam and item["source_exam"] != exam:
            continue
        if query.casefold().strip() not in (item["name"] + " " + item["memo"] + " " + item["id"]).casefold():
            continue
        if model and model not in model_names(item):
            continue
        if status == "실패 포함":
            if not item["totals"]["failed"]:
                continue
        elif status != "전체" and item["status"] != status:
            continue
        if len(dates) == 2 and not dates[0] <= korean_time(item["created_at"]).date() <= dates[1]:
            continue
        selected.append(item)
    return selected


def _select(label, options, key, **kwargs):
    if key in st.session_state and st.session_state[key] not in options:
        del st.session_state[key]
    return st.selectbox(label, options, key=key, **kwargs)


def _open_workshop(experiment, round_id=None):
    st.session_state["writing-experiment-open"] = {"id": experiment["id"], "round_id": round_id}
    st.switch_page(str(menu_page("writing_evaluation_app.py")))


def _save_metadata(storage, experiment_id):
    try:
        lab.update_experiment_metadata(storage, experiment_id,
            name=st.session_state[f"manager-name-{experiment_id}"],
            memo=st.session_state[f"manager-memo-{experiment_id}"],
            archived=st.session_state[f"manager-archive-{experiment_id}"])
    except ValueError as exc:
        st.session_state["writing-manager-notice"] = ("error", str(exc))
    else:
        st.session_state["writing-manager-notice"] = ("success", "관리 정보를 저장했습니다.")


def _overview(storage, experiment, summary):
    st.dataframe([{"단계": STAGES[stage], **{label: summary["counts"][stage][status]
        for status, label in STATUSES.items()}} for stage in STAGES], hide_index=True, width="stretch")
    if summary["stale"]:
        st.warning("10분 이상 응답 대기로 남은 호출이 있습니다. 실제 실행 중인 화면을 확인한 뒤 실험실에서 복구하세요.")
    st.caption("화면에서 한 항목씩 실행합니다. 화면 이동·앱 종료 시 중단될 수 있으며 완료 기록은 보존됩니다.")
    if st.button("이 실험 이어서 실행", key="writing-manager-resume", type="primary"):
        _open_workshop(experiment)
    st.caption("연결된 실험실에서 단계별 실행·실패 재시도·평가 차수 추가 버튼을 직접 누릅니다.")
    choices = [("답안 생성", experiment["generator"]), ("사전 판단", experiment["prior_model"])]
    choices += [(r["name"], r["evaluator"]) for r in experiment["rounds"]]
    st.dataframe([{"용도": label, "서비스": choice["provider"], "모델": choice["model"]}
                  for label, choice in choices], hide_index=True, width="stretch")
    with st.form(f"writing-manager-meta-{experiment['id']}"):
        st.text_input("실험 이름", experiment["name"], key=f"manager-name-{experiment['id']}")
        st.text_area("메모", experiment.get("memo", ""), key=f"manager-memo-{experiment['id']}")
        st.checkbox("보관", experiment.get("archived", False), key=f"manager-archive-{experiment['id']}")
        st.caption("보관은 기본 목록에서 숨깁니다. 기록을 삭제하거나 진행 중인 호출을 중단하지 않습니다.")
        st.form_submit_button("관리 정보 저장", on_click=_save_metadata, args=(storage, experiment["id"]))
    if st.checkbox("문항·생성 설정 보기", key=f"manager-config-{experiment['id']}"):
        render_question(WritingQuestion.model_validate(experiment["reference"]["question"]),
                        instruction=experiment["reference"].get("source_instruction"))
        st.json({key: experiment[key] for key in ("generator", "prior_model", "rubric", "generation_system", "prior_system")})


def _answers(experiment):
    query = st.text_input("답안 내용·ID 검색", key=f"manager-answer-search-{experiment['id']}").casefold().strip()
    level = st.selectbox("생성 목표 수준", ["전체", *lab.LEVELS], key=f"manager-answer-level-{experiment['id']}")
    cases = [c for c in experiment["cases"] if (level == "전체" or c["goal_level"] == level)
             and query in (c["id"] + " " + " ".join((c["answer"] or {}).values())).casefold()]
    if not cases:
        st.info("조건에 맞는 답안이 없습니다.")
        return
    st.dataframe([{"답안": c["id"], "생성 목표": c["goal_level"],
        "생성 상태": STATUSES[c["generation"]["status"]], "사전 판단 상태": STATUSES[c["prior"]["status"]],
        "사전 판단 수준": c.get("assessment", {}).get("level", "미완료"),
        "사전 예상": sum(c["assessment"]["scores"].values()) if c.get("assessment") else None,
        "답안 미리보기": " / ".join((c["answer"] or {}).values())[:160]} for c in cases], hide_index=True, width="stretch")
    selected = _select("답안 상세", [c["id"] for c in cases], f"manager-answer-{experiment['id']}")
    case = next(c for c in cases if c["id"] == selected)
    _answer_content(case)


def _answer_content(case):
    st.caption(f"{case['id']} · 생성 목표: {case['goal_level']} · {case['profile']} · "
               + ("사용자 수정 답안" if case["edited"] else "생성 답안"))
    if case["answer"]:
        for key, value in case["answer"].items():
            if value:
                st.text(SCORE_LABELS.get(key, "서술형 답안"))
                st.code(value, language=None)
    else:
        st.info("아직 생성되지 않은 답안입니다.")
    st.text("생성 의도: " + case["design_notes"])
    if case.get("assessment"):
        st.caption("사전 예상은 AI의 잠정 판단이며 사람 채점 점수가 아닙니다.")
        assessment = case["assessment"]
        st.write(f"사전 판단 수준: {assessment['level']} · 예상 합계 {sum(assessment['scores'].values())}점")
        st.dataframe([{"영역": SCORE_LABELS[key], "예상 점수": score, "사전 판단 근거": assessment["reasons"][key]}
                      for key, score in assessment["scores"].items()], hide_index=True, width="stretch")
        for reason in assessment["uncertainties"]:
            st.write("사전 판단의 불확실성: " + reason)


def _rounded_rows(rows):
    return [{key: round(value, 2) if isinstance(value, float) else value for key, value in row.items()} for row in rows]


def _result_charts(report, maximum):
    complete = [row for row in report["answers"] if row["요약 포함"] and row["사전 예상"] is not None]
    if not complete:
        return
    left, right = st.columns(2)
    with left:
        st.vega_lite_chart(complete, spec={"title": "사전 예상과 평가 평균", "height": 300,
            "mark": {"type": "point", "filled": True, "size": 90}, "encoding": {
                "x": {"field": "사전 예상", "type": "quantitative", "scale": {"domain": [0, maximum]}},
                "y": {"field": "평가 평균", "type": "quantitative", "scale": {"domain": [0, maximum]}},
                "color": {"field": "생성 목표", "type": "nominal", "sort": lab.LEVELS},
                "tooltip": [{"field": key} for key in ("답안", "생성 목표", "사전 예상")]
                    + [{"field": key, "type": "quantitative", "format": ".2f"} for key in ("평가 평균", "차이(실제−예상)")],
            }}, use_container_width=True)
    with right:
        ordered = sorted(complete, key=lambda row: abs(row["차이(실제−예상)"]), reverse=True)
        st.vega_lite_chart(ordered, spec={"title": "답안별 점수 차이 (평가−사전 예상)", "height": 300,
            "mark": "bar", "encoding": {
                "x": {"field": "답안", "type": "ordinal", "sort": [row["답안"] for row in ordered]},
                "y": {"field": "차이(실제−예상)", "type": "quantitative", "scale": {"zero": True}},
                "color": {"condition": {"test": "datum['차이(실제−예상)'] >= 0", "value": "#2f6fba"}, "value": "#d57832"},
                "tooltip": [{"field": "답안"}, {"field": "차이(실제−예상)", "type": "quantitative", "format": ".2f"}],
            }}, use_container_width=True)
    for rows, field, title in ((report["levels"], "생성 목표", "수준별 평균 점수"),
                                (report["criteria"], "영역", "영역별 평균 점수")):
        values = [{field: row[field], "구분": label, "점수": row[key], "답안 수": row["답안 수"]}
                  for row in rows for key, label in (("사전 예상 평균", "사전 예상"), ("평가 평균", "실제 평가"))]
        st.vega_lite_chart(values, spec={"title": title, "height": 260, "mark": "bar", "encoding": {
            "x": {"field": field, "type": "nominal", "sort": [row[field] for row in rows], "axis": {"labelAngle": 0}},
            "xOffset": {"field": "구분"}, "y": {"field": "점수", "type": "quantitative", "scale": {"zero": True}},
            "color": {"field": "구분", "type": "nominal"},
            "tooltip": [{"field": field}, {"field": "구분"}, {"field": "답안 수"},
                        {"field": "점수", "type": "quantitative", "format": ".2f"}],
        }}, use_container_width=True)


def _results(storage, experiment):
    rounds = {r["id"]: r for r in experiment["rounds"]}
    if not rounds:
        st.info("아직 평가 차수가 없습니다. 아래에서 생성 답안과 사전 판단 결과를 확인할 수 있습니다.")
        _answers(experiment)
        return
    round_id = _select("결과를 볼 평가 차수", list(rounds), f"manager-result-round-{experiment['id']}",
        index=len(rounds) - 1,
        format_func=lambda value: f"{rounds[value]['name']} · {rounds[value]['evaluator']['model']} · {value[:8]}")
    round_data = rounds[round_id]
    runs = lab.experiment_runs(storage, experiment["id"], round_ids=[round_id])
    report = lab.round_results(experiment, runs, round_id)
    summary = report["summary"]
    slot = experiment["reference"]["question"]["type_slot"]
    maximum = sum(SCORE_LIMITS[slot].values())
    st.caption(f"{slot}번 · {maximum}점 만점 · 답안별 {round_data['repeats']}회 반복. "
               "지정한 반복을 모두 완료한 답안만 요약·그래프에 포함하며 답안마다 같은 비중으로 계산합니다.")
    for labels in (("평균 사전 예상 점수", "평균 평가 점수", "완료 답안 수"),
                   ("평균 점수 차이", "평균 절대 차이", "최대 절대 차이")):
        for column, label in zip(st.columns(3), labels):
            value = summary[label]
            text = (f"{value} / {summary['전체 답안 수']}" if label == "완료 답안 수" else
                    "—" if value is None else f"{value:+.2f}점" if label == "평균 점수 차이" else f"{value:.2f}점")
            column.metric(label, text)
    st.caption(f"사전 예상 비교 대상 {summary['예상 비교 답안 수']}개. 차이 = 평가 평균 − AI 사전 예상. "
               "평균 절대 차이는 방향을 제외한 차이의 크기이며, 사람 채점 정확도를 뜻하지 않습니다.")
    if not summary["완료 답안 수"]:
        st.info("반복 평가를 모두 마친 답안이 없습니다. 부분 결과는 아래 표와 상세에서 확인하세요.")
    _result_charts(report, maximum)
    if report["levels"]:
        st.write("수준별·영역별 집계")
        st.dataframe(_rounded_rows(report["levels"]), hide_index=True, width="stretch")
        st.dataframe(_rounded_rows(report["criteria"]), hide_index=True, width="stretch")
    st.write("전체 답안 결과")
    ordered = sorted(report["answers"], key=lambda row: abs(row["차이(실제−예상)"])
                     if row["차이(실제−예상)"] is not None else -1, reverse=True)
    st.dataframe(_rounded_rows(ordered), hide_index=True, width="stretch")
    st.caption("부분 완료 행의 평균은 성공한 반복만의 잠정 값입니다. 실패·대기는 0점으로 계산하지 않습니다. "
               "성공한 반복이 1회뿐이면 점수 폭·표본 표준편차를 표시하지 않습니다.")
    st.download_button("실험 결과 요약 CSV 받기", csv_bytes(_rounded_rows([summary])),
                       f"writing_result_summary_{experiment['id']}_{round_id}.csv", "text/csv")
    st.download_button("전체 답안 결과 CSV 받기", csv_bytes(_rounded_rows(ordered)),
                       f"writing_results_{experiment['id']}_{round_id}.csv", "text/csv")
    st.subheader("개별 답안의 상세 평가")
    case_id = _select("결과를 확인할 답안", [row["답안"] for row in ordered], f"manager-result-case-{experiment['id']}")
    case = next(c for c in experiment["cases"] if c["id"] == case_id)
    _answer_content(case)
    repeats = [{**row, "상태": STATUSES[row["상태"]]} for row in report["repeats"] if row["답안"] == case_id]
    st.dataframe(repeats, hide_index=True, width="stretch")
    scored = [row for row in repeats if row["평가 점수"] is not None]
    if scored:
        st.vega_lite_chart(scored, spec={"title": "반복별 평가 점수", "height": 220,
            "mark": {"type": "point", "filled": True, "size": 100}, "encoding": {
                "x": {"field": "반복", "type": "ordinal", "scale": {"domain": list(range(1, round_data["repeats"] + 1))}},
                "y": {"field": "평가 점수", "type": "quantitative", "scale": {"domain": [0, maximum]}},
                "tooltip": [{"field": key} for key in ("반복", "평가 점수", "실행")],
            }}, use_container_width=True)
    by_run = {row["실행"]: row for row in repeats if row["실행"] is not None}
    if by_run:
        run_id = _select("상세 평가를 볼 반복 실행", list(by_run), f"manager-result-run-{experiment['id']}-{round_id}-{case_id}",
            format_func=lambda value: f"{by_run[value]['반복']}회 · {by_run[value]['상태']} · 최근 실행 #{value}")
        render_saved_evaluation(storage, runs[run_id])
    else:
        st.info("이 답안의 평가 호출 기록이 아직 없습니다.")
    with st.expander("이 차수의 모델·프롬프트·채점 기준"):
        st.json({key: round_data[key] for key in ("evaluator", "system", "notes", "rubric")})
    st.write("호출 자원과 등록된 사람 채점")
    _round_records(storage, experiment, [round_id], runs)


def _comparisons(storage, experiment):
    rounds = {r["id"]: r for r in experiment["rounds"]}
    if not rounds:
        st.info("아직 평가 차수가 없습니다. 사전 판단을 마친 뒤 실험실에서 평가 차수를 추가하세요.")
        return
    selected = st.multiselect("비교할 평가 차수", list(rounds), default=list(rounds)[-2:],
        format_func=lambda value: f"{rounds[value]['name']} · {rounds[value]['evaluator']['model']} · {value[:8]}",
        key=f"manager-rounds-{experiment['id']}")
    if not selected:
        st.info("비교할 평가 차수를 선택하세요.")
        return
    runs = lab.experiment_runs(storage, experiment["id"], round_ids=selected)
    summaries, rows = lab.round_comparison(experiment, runs, selected)
    st.caption(f"{experiment['reference']['question']['type_slot']}번 · 만점 "
               f"{sum(SCORE_LIMITS[experiment['reference']['question']['type_slot']].values())}점. "
               "선택 차수 모두에서 반복 평가를 완료한 공통 답안만 점수 요약에 포함합니다.")
    st.caption(f"공통 답안 {summaries[0]['공통 답안 수']} / {len(experiment['cases'])}개 · "
               f"기준 차수: {summaries[0]['평가 차수']} ({summaries[0]['차수 ID'][:8]}). "
               "사전 예상과의 차이는 채점 정확도를 뜻하지 않습니다.")
    st.dataframe(summaries, hide_index=True, width="stretch")
    st.download_button("차수 요약 CSV 받기", csv_bytes(summaries), f"writing_rounds_{experiment['id']}.csv", "text/csv")
    if rows:
        rows = sorted(rows, key=lambda row: abs(row["기준 차수와 차이"]), reverse=True)
        display = [{SCORE_LABELS.get(key, key): value for key, value in row.items()} for row in rows]
        st.dataframe(display, hide_index=True, width="stretch")
        st.download_button("답안별 비교 CSV 받기", csv_bytes(display), f"writing_comparison_{experiment['id']}.csv", "text/csv")
    else:
        st.info("공통 완료 답안이 아직 없습니다. 미완료·실패 답안은 0점으로 계산하지 않습니다.")
    _round_records(storage, experiment, selected, runs)
    chosen = _select("실험실에서 열 평가 차수", selected, f"manager-open-round-{experiment['id']}",
                     format_func=lambda value: f"{rounds[value]['name']} · {value[:8]}")
    if st.button("이 차수 실행·실패 재시도", key="writing-manager-round-resume"):
        _open_workshop(experiment, chosen)
    if st.checkbox("선택 차수 프롬프트 보기", key=f"manager-prompts-{experiment['id']}"):
        for round_id in selected:
            st.text(f"{rounds[round_id]['name']} · {round_id[:8]}")
            st.json({key: rounds[round_id][key] for key in ("system", "notes", "rubric")})


def _round_records(storage, experiment, selected, runs):
    rounds = {r["id"]: r for r in experiment["rounds"]}
    call_rows = [r for r in lab.experiment_run_summaries(storage, experiment["id"]) if r["round_id"] in selected]
    resources = []
    for round_id in selected:
        calls = [r for r in call_rows if r["round_id"] == round_id]
        resources.append({"차수 ID": round_id, "평가 차수": rounds[round_id]["name"], "호출 수(재시도 포함)": len(calls),
            "실패 호출 수": sum(bool(r["error"]) for r in calls),
            "실패 호출 비율(%)": round(100 * sum(bool(r["error"]) for r in calls) / len(calls), 1) if calls else None,
            "누적 호출 시간(초)": round(sum(r["duration_seconds"] or 0 for r in calls), 2),
            "기록된 입력 토큰": sum(r["input_tokens"] or 0 for r in calls),
            "기록된 출력 토큰": sum(r["output_tokens"] or 0 for r in calls),
            "토큰 누락 호출 수": sum(r["input_tokens"] is None or r["output_tokens"] is None for r in calls)})
    st.dataframe(resources, hide_index=True, width="stretch")
    st.caption("호출 자원은 실패·재시도를 포함합니다. 토큰 누락 호출은 합계에서 제외되며 비용은 추정하지 않습니다.")
    human_rows = []
    for round_id in selected:
        errors = []
        for task in rounds[round_id]["tasks"]:
            if task["status"] != "succeeded":
                continue
            review = human_review(storage, task["run_id"])
            result = json.loads(runs[task["run_id"]]["result_json"])
            if review and review.get("request_id") == result["request_parameters"]["request_id"]:
                total = EvaluationResult.model_validate(result["parsed_json"]).total
                errors.append(abs(total - sum(review["scores"].values())))
        if errors:
            human_rows.append({"차수 ID": round_id, "평가 차수": rounds[round_id]["name"],
                "사람 채점 실행 수": len(errors), "사람 점수와 평균 절대 차이": round(mean(errors), 2)})
    if human_rows:
        st.write("사람 채점이 등록된 실행의 비교")
        st.dataframe(human_rows, hide_index=True, width="stretch")
        st.caption("사람 채점 비교는 등록된 개별 실행 기준이며, 차수마다 대상 답안과 표본 수가 다를 수 있습니다.")
    else:
        st.caption("등록된 사람 채점이 없습니다. AI 사전 예상과의 비교는 사람 채점과 별도로 표시합니다.")


def _calls(storage, experiment):
    calls = lab.experiment_run_summaries(storage, experiment["id"])
    cols = st.columns(2)
    stage = cols[0].selectbox("호출 단계", ["전체", *STAGES], format_func=lambda value: STAGES.get(value, value))
    failed_only = cols[1].checkbox("실패 호출만")
    calls = [r for r in calls if (stage == "전체" or r["stage"] == stage) and (not failed_only or r["error"])]
    if not calls:
        st.info("조건에 맞는 호출 기록이 없습니다.")
        return
    page = _select("호출 페이지", list(range(1, (len(calls) - 1) // PAGE_SIZE + 2)), f"manager-call-page-{experiment['id']}")
    visible = calls[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    st.caption(f"{len(calls)}개 호출 · 실패 후 재시도한 이전 호출도 보존합니다. 시간은 한국 시간입니다.")
    st.dataframe([{"실행": r["id"], "단계": STAGES[r["stage"]], "답안": r["case_id"],
        "차수 ID": r["round_id"], "반복": r["repeat"], "서비스": r["provider"], "모델": r["model"],
        "상태": "실패" if r["error"] else "완료", "오류": r["error"],
        "시간": korean_time(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
        "소요 시간(초)": r["duration_seconds"], "입력 토큰": r["input_tokens"], "출력 토큰": r["output_tokens"]}
        for r in visible], hide_index=True, width="stretch")
    run_id = _select("호출 상세", [None, *[r["id"] for r in visible]], f"manager-call-{experiment['id']}",
                     format_func=lambda value: "선택하세요" if value is None else f"실행 #{value}")
    if run_id is not None:
        run = lab.experiment_run(storage, experiment["id"], run_id)
        if run["operation"] == "writing_evaluation":
            render_saved_evaluation(storage, run)
        else:
            st.code(run["prompt_system"], language=None)
            st.code(run["prompt_user"], language="json")
            st.code(run["result_json"], language="json")


def _export(storage, experiment):
    runs = lab.experiment_runs(storage, experiment["id"])
    reviews = {str(run_id): review for run_id in runs if (review := human_review(storage, run_id))}
    st.download_button("전체 실험 기록 JSON 받기", json.dumps({"experiment": experiment,
        "runs": list(runs.values()), "human_reviews": reviews}, ensure_ascii=False, indent=2),
        f"writing_experiment_{experiment['id']}.json", "application/json")
    st.caption("실험 설정·문항·답안·평가 차수·실패 및 재시도 기록·등록된 사람 채점을 포함합니다.")


def render_manager(storage):
    st.caption("유형과 기출 회차에 관계없이 저장된 실험을 조회합니다. 조회·화면 이동만으로 모델을 호출하지 않습니다.")
    notice = st.session_state.pop("writing-manager-notice", None)
    if notice:
        (st.error if notice[0] == "error" else st.success)(notice[1])
    if st.button("목록 새로고침", key="writing-manager-refresh"):
        st.rerun()
    items = lab.experiment_summaries(storage)
    if not items:
        st.info("저장된 실험이 없습니다. 평가 프롬프트 실험실의 대량 실험에서 새 실험을 만드세요.")
        return
    filters = st.columns(3)
    query = filters[0].text_input("실험명·메모·ID 검색", key="manager-search")
    slot = filters[1].selectbox("쓰기 유형", [None, 51, 52, 53, 54],
                               format_func=lambda value: "전체" if value is None else f"{value}번", key="manager-slot")
    exam = filters[2].selectbox("기출 회차", [None, *sorted({i["source_exam"] for i in items}, reverse=True)],
                               format_func=lambda value: "전체" if value is None else f"{value}회", key="manager-exam")
    filters = st.columns(3)
    status = filters[0].selectbox("처리 상태", ["전체", "대기", "실행 응답 대기", "실행 상태 확인 필요", "실패 포함", "평가 준비 완료", "평가 완료"], key="manager-status")
    model = filters[1].selectbox("사용 모델 (모든 단계)", [None, *sorted(set().union(*(model_names(i) for i in items)))],
                                format_func=lambda value: value or "전체", key="manager-model")
    archive = filters[2].selectbox("보관 상태", ["보관 제외", "보관 포함", "보관만"], key="manager-archive")
    dates = ()
    if st.checkbox("생성 기간 지정 (한국 시간)", key="manager-date-filter"):
        dates = st.date_input("생성 기간", value=(min(korean_time(i["created_at"]).date() for i in items), datetime.now(KST).date()), key="manager-dates")
        if len(dates) != 2:
            st.info("시작일과 종료일을 모두 선택하세요.")
            return
    filtered = filter_summaries(items, query=query, slot=slot, exam=exam, status=status, model=model, archive=archive, dates=dates)
    metrics = st.columns(4)
    for column, label, value in zip(metrics, ["실험 수", "응답 대기 포함", "실패 포함", "평가 완료"],
            [len(filtered), sum(bool(i["totals"]["running"]) for i in filtered),
             sum(bool(i["totals"]["failed"]) for i in filtered), sum(i["status"] == "평가 완료" for i in filtered)]):
        column.metric(label, value)
    st.caption("현재 검색·필터 조건 기준입니다. 시간은 한국 시간입니다.")
    if not filtered:
        st.info("조건에 맞는 실험이 없습니다.")
        return
    order = st.selectbox("정렬", ["최근 변경순", "최근 생성순", "실험명순"], key="manager-sort")
    sort_key = {"최근 변경순": "updated_at", "최근 생성순": "created_at", "실험명순": "name"}[order]
    filtered.sort(key=lambda i: i[sort_key], reverse=order != "실험명순")
    page = _select("목록 페이지", list(range(1, (len(filtered) - 1) // PAGE_SIZE + 2)), "manager-page")
    visible = filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    rows = []
    for item in visible:
        row = {"실험명": item["name"], "문항": item["source_key"], "답안 수": item["case_count"],
               "진행 단계": item["stage"], "처리 상태": item["status"], "평가 차수": item["round_count"],
               "실패": item["totals"]["failed"], "보관": bool(item["archived"]),
               "최근 변경": korean_time(item["updated_at"]).strftime("%Y-%m-%d %H:%M:%S"), "실험 ID": item["id"]}
        for stage, label in STAGES.items():
            counts = item["counts"][stage]
            row[label] = f"{counts['succeeded']}/{sum(counts.values())}"
        rows.append(row)
    st.dataframe(rows, hide_index=True, width="stretch")
    by_id = {item["id"]: item for item in visible}
    selected = _select("상세 조회할 실험", [None, *by_id], "writing-manager-selected",
        format_func=lambda value: "선택하세요" if value is None else f"{by_id[value]['name']} · {by_id[value]['source_key']} · {value[:8]}")
    if selected is None:
        return
    experiment = lab.load_experiment(storage, selected)
    st.subheader(experiment["name"])
    section = st.radio("상세 보기", ["실험 결과", "실험 개요", "답안 목록", "평가 차수 비교", "호출·오류 기록", "내보내기"],
                       horizontal=True, key=f"manager-section-{selected}")
    if section == "실험 결과":
        _results(storage, experiment)
    elif section == "실험 개요":
        _overview(storage, experiment, by_id[selected])
    elif section == "답안 목록":
        _answers(experiment)
    elif section == "평가 차수 비교":
        _comparisons(storage, experiment)
    elif section == "호출·오류 기록":
        _calls(storage, experiment)
    else:
        _export(storage, experiment)
