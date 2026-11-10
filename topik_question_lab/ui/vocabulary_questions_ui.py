"""Word exercises and durable batch controls for the exercise page."""
import streamlit as st

from topik_question_lab.common.vocabulary_models import VocabularyBundle
from topik_question_lab.step_03_generation.bulk_generation import model_options, custom_model
from topik_question_lab.step_03_generation.providers import can_gateway_call
from topik_question_lab.step_03_generation.vocabulary_bulk import VocabularyRunner
from topik_question_lab.step_04_review.vocabulary_validation import validation_retry_details
from topik_question_lab.ui.vocabulary_prompt_ui import render_prompt_editor, render_prompt_history
from topik_question_lab.step_06_publication.vocabulary_questions import (
    VocabularyQuestionBank, prepare_question_schema, question_status,
)


LABELS = {"pending": "생성 대기", "processing": "처리 중", "retry_wait": "재시도 대기", "ready": "검수 완료",
          "needs_attention": "확인 필요", "failed": "미완료", "skipped": "생략", "saved": "저장 완료",
          "running": "실행 중", "paused": "중단", "partial": "일부 완료", "completed": "저장 완료"}
TYPES = {"sentence_blank": "문장 빈칸", "conjugation": "활용형", "synonym": "유의어", "antonym": "반의어"}
DEFAULT_VOCABULARY_MODEL = ("bizrouter:openai/gpt-6-luna", "openai/gpt-6-luna")


@st.cache_resource
def get_vocabulary_runner(root):
    """Cache the runner that automatically publishes accepted word exercises."""
    return VocabularyRunner(root)


def preview(questions, word, *, key):
    mode = st.radio("빈칸 표시 방식", ["객관식", "주관식"], horizontal=True, key=key)
    for q in questions:
        st.markdown(f"**{q['position']}. {TYPES[q['question_type']]}**")
        subjective = q["question_type"] == "sentence_blank" and mode == "주관식"
        st.write(f"‘{word}’를 알맞게 바꾸어 빈칸을 채우세요." if subjective else q["instruction"])
        st.write(q["stem"])
        if not subjective:
            for index, choice in enumerate(q["choices"], 1):
                st.write(f"{index}. {choice}")
        with st.expander("정답·해설"):
            st.write("허용 답안: " + ", ".join(q["accepted_answers"]) if subjective
                     else f"정답: {q['correct_answer']}. {q['choices'][q['correct_answer'] - 1]}")
            st.write(q["explanation"])


@st.fragment(run_every="5s")
def render_progress(url):
    st.subheader("전체 단어 문제 생성 현황")
    if not url:
        st.caption("PostgreSQL 연결을 설정하면 완료·남은 단어 수를 표시합니다.")
        return
    try:
        counts = VocabularyQuestionBank(url).progress()
    except ValueError as exc:
        st.warning(str(exc))
        return
    total, completed, remaining = st.columns(3)
    total.metric("전체 단어", f"{counts['total']:,}")
    completed.metric("저장 완료 단어", f"{counts['completed']:,}")
    remaining.metric("남은 단어", f"{counts['remaining']:,}")
    st.progress(counts["completed"] / counts["total"] if counts["total"] else 0)
    st.caption("전체 단어장 기준 · PostgreSQL에 유효한 문제 묶음이 저장되면 완료로 집계합니다. "
               "미생성·미완료·갱신 필요 단어는 남은 단어에 포함되며, 5초마다 갱신합니다.")


@st.fragment(run_every="2s")
def render_batches(runner, bank, database_key):
    runner.recover_idle()
    batches = runner.store.batches(database_key)
    st.subheader("4. 실행 결과")
    if not batches:
        st.info("생성·검수를 시작하면 결과가 표시됩니다. 통과한 문제는 PostgreSQL에 자동 저장됩니다.")
        return
    batch_ids = [b["batch_id"] for b in batches]
    by_id = {b["batch_id"]: b for b in batches}
    if st.session_state.get("vocab-batch") not in batch_ids:
        st.session_state.pop("vocab-batch", None)
    batch_id = st.selectbox("생성 작업", batch_ids, key="vocab-batch",
        format_func=lambda value: f"{by_id[value]['created_at']} UTC · {LABELS[by_id[value]['status']]} · {value[:8]}")
    batch = by_id[batch_id]
    tasks = runner.store.tasks(batch_id)
    saved = sum(t["status"] == "saved" for t in tasks)
    st.caption(f"작업 상태: {LABELS[batch['status']]} · 모델: {batch['config']['model']}")
    st.progress(saved / len(tasks), text=f"이번 실행: {len(tasks)}단어 중 {saved}단어 자동 저장")
    if batch["error"]:
        st.warning(batch["error"])
    active = runner.active and runner.active_batch == batch_id
    if active:
        st.info("생성 → 검수 → 필요 시 수정·최종 검수 → PostgreSQL 자동 저장 순서로 진행 중입니다.")
        if st.button("현재 실행 중단", key="vocab-stop"):
            runner.stop()
            st.info("진행 중인 응답을 기록한 뒤 중단합니다. 미완료 단어는 다음 생성에서 처음부터 시작합니다.")
    elif saved == len(tasks):
        st.success(f"이번 실행의 {saved}단어를 PostgreSQL에 자동 저장했습니다.")
    else:
        st.info(f"이번 실행에서 저장하지 못한 {len(tasks) - saved}단어는 초기 상태입니다. "
                "다음에 ‘생성 시작’을 누르면 미완료 단어를 처음부터 생성합니다.")
    st.caption("실패·중단된 시도의 문항과 오류는 아래 이력에 보존합니다. "
               "저장된 문항의 수정·삭제는 ‘단어 문제 관리’ 메뉴에서 할 수 있습니다.")
    render_results(tasks, batch_id)


def render_results(tasks, batch_id):
    status = st.selectbox("작업 상태", ["all", *[key for key in LABELS if key in {t['status'] for t in tasks}]],
                          format_func=lambda v: "전체" if v == "all" else LABELS[v], key=f"vocab-status-{batch_id}")
    filtered = [t for t in tasks if status == "all" or t["status"] == status]
    st.dataframe([{"단어": t["snapshot"]["word"]["korean"], "상태": LABELS[t["status"]],
                   "단계": {"generate": "최초 생성", "review": "최종 검수" if t["repair_count"] else "1차 검수",
                            "repair": "지적 문항 수정", "save": "저장"}[t["stage"]],
                   "자동 수정 횟수": t["checkpoint"].get("last_attempt", {}).get("repair_count", t["repair_count"]),
                   "AI 호출 수": len(t["checkpoint"].get("calls", [])),
                   "메시지": t["error"]} for t in filtered], hide_index=True)
    if not filtered:
        return
    by_task = {t["generation_id"]: t for t in filtered}
    task_key = f"vocab-preview-{batch_id}-{status}"
    if st.session_state.get(task_key) not in by_task:
        st.session_state.pop(task_key, None)
    selected_id = st.selectbox("문제 미리보기", list(by_task), format_func=lambda v: by_task[v]["snapshot"]["word"]["korean"], key=task_key)
    task = by_task[selected_id]
    st.caption(f"선택 단어 상태: {LABELS[task['status']]} · 실제 AI 호출: {len(task['checkpoint'].get('calls', []))}회")
    if task["error"]:
        st.warning(task["error"])
    for issue in task["checkpoint"].get("issues", []):
        st.warning(issue)
    for warning in task["checkpoint"].get("review", {}).get("warnings", []):
        st.info("개선 권고: " + warning)
    render_prompt_history(task)
    details = validation_retry_details(task)
    if details and not task["checkpoint"].get("issues"):
        for message in details[0]:
            st.warning(message)
    candidate = task["checkpoint"].get("candidate")
    if isinstance(candidate, dict):
        try:
            bundle = VocabularyBundle.model_validate(candidate)
        except ValueError:
            st.warning("문항 구조 확인이 필요합니다. 1단계에서 이 단어를 선택하고 3단계에서 새 작업으로 생성하세요.")
        else:
            st.caption("출제 의미: " + bundle.target_meaning)
            if bundle.skipped_reason:
                st.info("유의어·반의어 생략: " + bundle.skipped_reason)
            preview([q.model_dump() for q in bundle.questions], task["snapshot"]["word"]["korean"], key=f"vocab-mode-{selected_id}")


def render_question_controls(url, root, selected, *, level, query):
    bank = VocabularyQuestionBank(url)
    try:
        if not bank.available():
            st.info("문제 기능 준비를 누르면 로컬 DB를 백업하고 단어 문제 테이블을 추가합니다.")
            if st.button("문제 기능 준비", key="vocab-schema"):
                prepare_question_schema(root, url)
                st.rerun()
            return
        database_key = bank.identity()
        runner = get_vocabulary_runner(str(root))
        if selected:
            current = bank.snapshot(selected["vocabulary_id"])
            state = question_status(current)
            st.caption("선택 단어 문제: " + {"missing": "없음", "ready": "저장됨", "stale": "갱신 필요"}[state])
            if current["existing"]:
                with st.expander("저장된 문제 보기"):
                    preview(current["existing"], selected["korean"], key=f"vocab-saved-mode-{selected['vocabulary_id']}")
        with st.expander("문제 생성 설정", expanded=True):
            models = [DEFAULT_VOCABULARY_MODEL, *[option for option in model_options(root) if option != DEFAULT_VOCABULARY_MODEL]]
            provider, model = st.selectbox("문제 생성·검수 모델", models,
                format_func=lambda v: "gpt-6-luna · BizRouter" if v == DEFAULT_VOCABULARY_MODEL else f"{v[1]} · {v[0]}",
                key="vocab-model-default-gpt6")
            custom = st.text_input("직접 입력할 모델 ID (선택)", key="vocab-custom-model-default-gpt6")
            if custom.strip():
                provider, model = custom_model(custom)
            scope = st.radio("생성 범위", ["선택 단어", "검색 결과"], horizontal=True, key="vocab-scope")
            if st.session_state.get("vocab-target") not in (None, "unfinished", "regenerate"):
                st.session_state["vocab-target"] = "unfinished"
            mode = st.selectbox("생성 대상", ["unfinished", "regenerate"], key="vocab-target",
                                format_func=lambda v: {"unfinished": "미완료 단어", "regenerate": "기존 문제 포함 재생성"}[v])
            if mode == "regenerate":
                st.warning("재생성한 묶음이 검수를 통과하면 해당 단어의 기존 PostgreSQL 문제를 바로 교체합니다.")
            all_words = st.checkbox("검색 결과 전체", key="vocab-all", disabled=scope != "검색 결과")
            limit = st.number_input("생성할 단어 수", min_value=1, value=100, step=10, disabled=all_words or scope == "선택 단어", key="vocab-limit")
            concurrency = st.number_input("동시 처리 단어 수", min_value=1, max_value=3, value=2, key="vocab-concurrency")
            st.caption("빈칸 3개 + 동사·형용사 활용형 3개 + 유의어/반의어 최대 1개 = 단어당 최대 7개. 명사 등은 3~4개입니다.")
            st.caption("빈칸의 객관식·주관식은 같은 문항의 표시 방식이며, 중복 저장하지 않습니다.")
            st.caption("최초 생성 후 정답 위치를 섞습니다. 7문제는 ①~④에 2·2·2·1개로 배치하고, 자동 수정 시에도 배치를 유지합니다.")
            st.caption("화면을 이동해도 앱이 실행 중이면 계속 생성합니다. 중단·종료한 미완료 단어는 다음 생성에서 처음부터 시작합니다.")
            if not can_gateway_call(provider):
                st.info("선택 모델의 API 연결 설정이 필요합니다.")
        prompts = render_prompt_editor(database_key, selected)
        st.subheader("3. 생성·검수 실행")
        st.write("생성 → 1차 검수: 통과하면 2회로 완료합니다. 실패하면 지적 문항 수정 → 전체 최종 검수를 더해 최대 4회 실행합니다.")
        st.caption("통과하면 PostgreSQL에 자동 저장합니다. 실패하면 초기 상태로 돌아가 다음 생성에서 처음부터 시작합니다. "
                   "통신 오류 재시도(단계별 최대 2회)와 다음 생성 실행은 별도 호출입니다.")
        target_label = selected["korean"] if scope == "선택 단어" and selected else "검색 결과"
        st.caption(f"실행 대상: {target_label} · 검수 통과 시 자동 저장하며, 4단계에서 결과를 확인할 수 있습니다.")
        if st.button("생성 시작 · 통과 시 자동 저장", type="primary", key="vocab-start", disabled=runner.active or prompts is None or not can_gateway_call(provider) or (scope == "선택 단어" and not selected)):
            with st.spinner("대상 단어와 기존 문항 확인 중"):
                snapshots = bank.select(level=level, query=query, mode=mode,
                    limit=1 if scope == "선택 단어" else None if all_words else int(limit),
                    ids=[selected["vocabulary_id"]] if scope == "선택 단어" else None)
                config = {"provider": provider, "model": model, "concurrency": int(concurrency), **prompts,
                          "selection": {"level": level, "query": query, "mode": mode, "scope": scope}}
                batch_id = runner.start(url, snapshots=snapshots, config=config)
            st.session_state["vocab-batch"] = batch_id
            st.rerun()
        render_batches(runner, bank, database_key)
    except (ValueError, RuntimeError) as exc:
        st.error(str(exc))
