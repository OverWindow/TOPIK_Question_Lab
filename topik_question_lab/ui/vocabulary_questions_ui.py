"""Word exercises and durable batch controls for the exercise page."""
import streamlit as st

from topik_question_lab.common.vocabulary_models import VocabularyBundle, digest
from topik_question_lab.step_03_generation.bulk_generation import model_options, custom_model
from topik_question_lab.step_03_generation.providers import can_gateway_call
from topik_question_lab.step_03_generation.vocabulary_bulk import VocabularyRunner
from topik_question_lab.step_04_review.vocabulary_validation import validation_retry_details
from topik_question_lab.ui.vocabulary_prompt_ui import render_prompt_editor, render_prompt_history
from topik_question_lab.step_06_publication.vocabulary_questions import (
    VocabularyQuestionBank, prepare_question_schema, question_status,
)


LABELS = {"pending": "대기", "processing": "처리 중", "retry_wait": "재시도 대기", "ready": "검수 완료",
          "needs_attention": "확인 필요", "failed": "실패", "skipped": "생략", "saved": "저장 완료",
          "running": "실행 중", "paused": "일시정지", "partial": "일부 완료", "completed": "저장 완료"}
TYPES = {"sentence_blank": "문장 빈칸", "conjugation": "활용형", "synonym": "유의어", "antonym": "반의어"}
DEFAULT_VOCABULARY_MODEL = ("bizrouter:openai/gpt-6-luna", "openai/gpt-6-luna")


@st.cache_resource
def get_vocabulary_runner(root):
    """Cache the runner preserving editable prompt snapshots and verified publication."""
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


def editor(runner, task):
    candidate = task["checkpoint"].get("candidate")
    if not candidate or task["status"] == "saved":
        return
    try:
        bundle = VocabularyBundle.model_validate(candidate).model_dump()
    except ValueError:
        st.info("응답 구조를 복원할 수 없습니다. 이 단어를 새 작업으로 생성하세요.")
        return
    key = f"vocab-edit-{task['generation_id']}-{digest(candidate)[:12]}"
    with st.expander("문항 수정 후 재검수"):
        with st.form(key):
            bundle["target_meaning"] = st.text_input("출제 의미", value=bundle["target_meaning"])
            bundle["skipped_reason"] = st.text_input("유의어·반의어 생략 이유", value=bundle["skipped_reason"])
            for q in bundle["questions"]:
                pos = q["position"]
                st.markdown(f"**{pos}번 수정**")
                if pos == 7:
                    q["question_type"] = st.selectbox("관계", ["synonym", "antonym"],
                        index=0 if q["question_type"] == "synonym" else 1, format_func=TYPES.get, key=f"{key}-{pos}-type")
                for field, label in (("instruction", "지시문"), ("stem", "본문"), ("explanation", "해설")):
                    q[field] = st.text_area(label, value=q[field], key=f"{key}-{pos}-{field}")
                choices = st.text_area("보기 (한 줄에 하나씩 4개)", value="\n".join(q["choices"]), key=f"{key}-{pos}-choices")
                q["choices"] = choices.splitlines()
                q["correct_answer"] = st.number_input("정답 번호", min_value=1, max_value=4, value=q["correct_answer"], key=f"{key}-{pos}-answer")
                if q["question_type"] == "sentence_blank":
                    answers = st.text_area("허용 답안 (한 줄에 하나)", value="\n".join(q["accepted_answers"]), key=f"{key}-{pos}-accepted")
                    q["accepted_answers"] = answers.splitlines()
            submit = st.form_submit_button("수정본 반영 · 검수 대기로 이동", disabled=runner.active)
        if submit:
            try:
                runner.edit(task["generation_id"], bundle)
                st.success("수정본을 기록했습니다. 재개 버튼으로 검수하세요.")
                st.rerun()
            except ValueError:
                st.error("문항 수·위치·보기·정답·허용 답안을 확인하세요.")


@st.fragment(run_every="2s")
def render_batches(runner, bank, database_key):
    runner.recover_idle()
    batches = runner.store.batches(database_key)
    if not batches:
        return
    st.markdown("**단어 문제 생성 이력**")
    batch_ids = [b["batch_id"] for b in batches]
    by_id = {b["batch_id"]: b for b in batches}
    if st.session_state.get("vocab-batch") not in batch_ids:
        st.session_state.pop("vocab-batch", None)
    batch_id = st.selectbox("생성 작업", batch_ids, key="vocab-batch",
        format_func=lambda value: f"{by_id[value]['created_at']} UTC · {LABELS[by_id[value]['status']]} · {value[:8]}")
    batch = by_id[batch_id]
    tasks = runner.store.tasks(batch_id)
    st.caption(f"작업 상태: {LABELS[batch['status']]} · 모델: {batch['config']['model']}")
    complete = sum(t["status"] in {"ready", "saved", "skipped"} for t in tasks)
    st.progress(complete / len(tasks), text=f"검수 완료 {complete}/{len(tasks)}단어 · 저장 {sum(t['status']=='saved' for t in tasks)}단어")
    if batch["error"]:
        st.warning(batch["error"])
    actions = st.columns(2)
    if actions[0].button("일시정지", key="vocab-pause", disabled=not runner.active or runner.active_batch != batch_id):
        runner.stop()
        st.info("진행 중인 응답을 기록한 뒤 일시정지합니다.")
    repairable = {t["generation_id"] for t in tasks if validation_retry_details(t) is not None}
    resumable = bool(repairable) or any(t["stage"] != "save" and t["status"] in {"pending", "processing", "failed", "retry_wait"} for t in tasks)
    if repairable and not runner.active:
        st.warning(f"{len(repairable)}단어가 문항 형식 검증에서 종료되었습니다. 재개하면 저장된 응답의 오류 문항만 수정·검수합니다.")
    if actions[1].button("재개 · 실패 단계 재시도", key="vocab-resume", disabled=runner.active or not resumable):
        try:
            runner.start(bank.url, batch_id=batch_id)
            st.rerun()
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))
    st.markdown("**PostgreSQL에 문제 저장**")
    saveable = [t for t in tasks if t["stage"] == "save" and t["status"] in {"ready", "pending", "processing", "failed"}]
    replacements = sum(bool(t["snapshot"]["existing"]) for t in saveable)
    question_count = sum(len(t["checkpoint"]["candidate"]["questions"]) for t in saveable)
    st.caption("저장 위치: PostgreSQL · topik_bank.vocabulary_questions")
    st.write(f"저장 대기: {len(saveable)}단어 / {question_count}문항 · 저장 완료: {sum(t['status']=='saved' for t in tasks)}단어")
    if replacements:
        st.warning(f"기존 문제 묶음 {replacements}개를 교체합니다. 새 묶음에서 생략한 기존 문항은 제거됩니다.")
    if not saveable and any(t["status"] not in {"saved", "skipped"} for t in tasks):
        st.info("저장할 검수 완료 문제가 없습니다. 위의 재개 버튼으로 생성·검수를 완료한 뒤 저장하세요.")
    notice_key = f"vocab-publication-notice-{database_key}-{batch_id}"
    if notice := st.session_state.get(notice_key):
        if notice.get("saved_words", 0):
            st.success(notice["message"])
        else:
            st.error("PostgreSQL에 저장 완료한 문제가 없습니다.")
        if notice["failures"]:
            st.warning(f"{notice['failures']}단어는 저장되지 않았습니다. 아래 작업 상태와 오류를 확인하세요.")
    if st.button("검수 완료 문제를 PostgreSQL에 저장", type="primary", key="vocab-save", disabled=runner.active or not saveable):
        try:
            with st.spinner("PostgreSQL에 문항을 저장하고 결과를 확인하는 중…"):
                results = runner.save_ready(bank.url, batch_id)
            saved_ids = {word_id for word_id, status in results if status == "saved"}
            saved_count = sum(len(t["checkpoint"]["candidate"]["questions"]) for t in saveable if t["vocabulary_id"] in saved_ids)
            st.session_state[notice_key] = {
                "message": f"PostgreSQL 저장 확인 완료: {len(saved_ids)}단어 / {saved_count}문항",
                "saved_words": len(saved_ids),
                "failures": sum(status != "saved" for _, status in results),
            }
            st.rerun()
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))
    status = st.selectbox("작업 상태", ["all", *[key for key in LABELS if key in {t['status'] for t in tasks}]],
                          format_func=lambda v: "전체" if v == "all" else LABELS[v], key=f"vocab-status-{batch_id}")
    filtered = [t for t in tasks if status == "all" or t["status"] == status]
    st.dataframe([{"단어": t["snapshot"]["word"]["korean"], "상태": LABELS[t["status"]],
                   "단계": {"generate": "생성", "review": "검수", "repair": "수정", "save": "저장"}[t["stage"]],
                   "메시지": t["error"]} for t in filtered], hide_index=True)
    if not filtered:
        return
    by_task = {t["generation_id"]: t for t in filtered}
    task_key = f"vocab-preview-{batch_id}-{status}"
    if st.session_state.get(task_key) not in by_task:
        st.session_state.pop(task_key, None)
    selected_id = st.selectbox("문제 미리보기", list(by_task), format_func=lambda v: by_task[v]["snapshot"]["word"]["korean"], key=task_key)
    task = by_task[selected_id]
    render_prompt_history(task)
    details = validation_retry_details(task)
    if details:
        for message in details[0]:
            st.warning(message)
    candidate = task["checkpoint"].get("candidate")
    if isinstance(candidate, dict):
        try:
            bundle = VocabularyBundle.model_validate(candidate)
        except ValueError:
            st.warning("문항 구조 확인이 필요합니다. 이 단어를 새 작업으로 생성하세요.")
        else:
            st.caption("출제 의미: " + bundle.target_meaning)
            if bundle.skipped_reason:
                st.info("유의어·반의어 생략: " + bundle.skipped_reason)
            preview([q.model_dump() for q in bundle.questions], task["snapshot"]["word"]["korean"], key=f"vocab-mode-{selected_id}")
            editor(runner, task)


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
            mode = st.selectbox("생성 대상", ["missing", "stale", "regenerate"], key="vocab-target",
                                format_func=lambda v: {"missing": "문제 없는 단어", "stale": "갱신 필요한 단어", "regenerate": "기존 문제 포함 재생성"}[v])
            all_words = st.checkbox("검색 결과 전체", key="vocab-all", disabled=scope != "검색 결과")
            limit = st.number_input("생성할 단어 수", min_value=1, value=100, step=10, disabled=all_words or scope == "선택 단어", key="vocab-limit")
            concurrency = st.number_input("동시 처리 단어 수", min_value=1, max_value=3, value=2, key="vocab-concurrency")
            st.caption("빈칸 3개 · 활용 가능한 단어는 활용형 3개 · 유의어/반의어 최대 1개. 단어당 기본 모델 요청 2회입니다.")
            st.caption("화면을 이동해도 앱이 실행 중이면 계속 생성합니다. 앱을 종료하면 다음 실행에서 재개할 수 있습니다.")
            if not can_gateway_call(provider):
                st.info("선택 모델의 API 연결 설정이 필요합니다.")
        prompts = render_prompt_editor(database_key, selected)
        if st.button("단어 문제 생성 시작", key="vocab-start", disabled=runner.active or prompts is None or not can_gateway_call(provider) or (scope == "선택 단어" and not selected)):
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
