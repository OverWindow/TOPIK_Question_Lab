from topik_question_lab.common.paths import application_root
import os

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.menus.vocabulary_transfer.vocabulary_transfer import TransferError, capture_vocabulary, digest, execute_vocabulary_transfer, prepare_vocabulary_transfer
from topik_question_lab.menus.vocabulary_transfer.vocabulary_question_transfer import capture_questions, execute_question_transfer, prepare_question_transfer


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="서버에 단어장·문제 등록", page_icon="📡", layout="wide")
st.title("서버에 단어장·문제 등록")
content = st.radio("등록 내용", ["단어장", "단어 연습문제"], horizontal=True)
include_questions = content == "단어 연습문제"
if include_questions:
    st.caption("전체 단어장과 유효한 문제 묶음을 로컬 원본에 맞춰 동기화합니다. "
               "미생성·불완전한 묶음은 제외하며, 해당 단어의 기존 서버 문제도 삭제됩니다.")
else:
    st.caption("생성기의 3~6급 전체 단어장을 선택 서버에 동기화합니다. 단어 ID를 기준으로 원본 내용으로 덮어씁니다.")

source = os.getenv("DATABASE_URL", "").strip()
targets = {
    "로컬 서버": os.getenv("QUESTION_BANK_LOCAL_DATABASE_URL", "").strip(),
    "운영 서버": os.getenv("QUESTION_BANK_PRODUCTION_DATABASE_URL", "").strip(),
}
selection = st.multiselect("등록 대상", list(targets), default=["로컬 서버"])
config_key = digest(("full-sync-v1", source, targets, content))
if st.session_state.get("vocabulary-transfer-config") != config_key:
    st.session_state["vocabulary-transfer-config"] = config_key
    st.session_state.pop("vocabulary-transfer-preview", None)
    st.session_state.pop("vocabulary-transfer-results", None)
configured = bool(source and selection and all(targets[label] for label in selection))
if not configured:
    st.info("DATABASE_URL과 선택 대상의 QUESTION_BANK_LOCAL_DATABASE_URL / QUESTION_BANK_PRODUCTION_DATABASE_URL을 설정하세요.")
if "운영 서버" in selection:
    st.warning("운영 서버가 선택되어 있습니다. 등록 버튼을 누르면 운영 DB에 적용됩니다.")
st.caption("항상 로컬 원본으로 덮어씁니다. 빈 번역·음성 연결값도 그대로 반영하며, "
           "로컬에 없는 서버 단어와 그에 연결된 문제는 삭제합니다. 변경 전 백업을 만듭니다.")
if include_questions:
    st.caption("같은 단어 ID·문항 위치의 기존 문제 ID는 유지합니다. 로컬 유효 묶음에 없는 서버 문제는 삭제합니다.")

request_key = digest((config_key, selection))
if st.button("등록 내용 확인", disabled=not configured):
    st.session_state.pop("vocabulary-transfer-preview", None)
    try:
        with st.spinner("원본과 대상 단어장을 확인합니다."):
            snapshot = capture_questions(source) if include_questions else capture_vocabulary(source)
            plans, errors, destinations = {}, {}, {}
            for label in selection:
                try:
                    plan = (prepare_question_transfer(snapshot, targets[label]) if include_questions
                            else prepare_vocabulary_transfer(snapshot, targets[label]))
                    identity = plan.vocabulary.target_identity if include_questions else plan.target_identity
                    destination = digest((identity["cluster"] or (identity["address"], identity["port"]), identity["database"]))
                    if destination in destinations:
                        errors[label] = f"{destinations[destination]}와 같은 대상 DB입니다. 한 대상만 선택하세요."
                    else:
                        destinations[destination] = label
                        plans[label] = plan
                except TransferError as exc:
                    errors[label] = str(exc)
            st.session_state["vocabulary-transfer-preview"] = (request_key, snapshot, plans, errors)
    except TransferError as exc:
        st.error(str(exc))

preview = st.session_state.get("vocabulary-transfer-preview")
if preview and preview[0] == request_key:
    _, snapshot, plans, errors = preview
    word_snapshot = snapshot.vocabulary if include_questions else snapshot
    st.subheader("생성기 원본")
    metrics = st.columns(5)
    metrics[0].metric("전체 단어", f"{len(word_snapshot.rows):,}")
    for column, level in zip(metrics[1:], range(3, 7)):
        column.metric(f"{level}급", f"{word_snapshot.levels[level]:,}")
    if include_questions:
        st.write(f"등록 대상 문제 {len(snapshot.questions):,}개 · 제외 단어 {len(snapshot.excluded):,}개")
        if snapshot.excluded:
            with st.expander("제외 단어와 사유"):
                st.dataframe(snapshot.excluded, hide_index=True, width="stretch")
    for label, message in errors.items():
        st.error(f"{label}: {message}")
    for label, plan in plans.items():
        word_plan = plan.vocabulary if include_questions else plan
        effects = word_plan.effects
        st.subheader(label)
        st.write("단어장 테이블을 새로 만들고 등록합니다." if word_plan.create_table else "기존 단어장을 로컬 원본과 동기화합니다.")
        if word_plan.upgrade_audio:
            st.info("기존 대상 단어장을 백업한 뒤 음성 저장 컬럼을 추가합니다.")
        if word_plan.upgrade_languages:
            st.info("기존 대상 단어장을 백업한 뒤 중국어(간체)·인도네시아어 뜻 컬럼을 추가합니다.")
        if "audio_created" in effects:
            st.caption(f"음성 신규 연결 {effects['audio_created']:,}개 · 음성 변경 {effects['audio_updated']:,}개 · "
                       "원본의 빈 음성 연결값은 서버에서도 해제됩니다. S3 파일은 공유합니다.")
        st.dataframe([{"현재 단어": word_plan.target_count, "신규": effects["created"], "변경": effects["updated"],
                       "동일": effects["unchanged"], "삭제": effects["deleted"]}], hide_index=True, width="stretch")
        if not include_questions and word_plan.deleted_questions:
            st.caption(f"삭제할 단어에 연결된 문제 {word_plan.deleted_questions:,}개도 함께 삭제합니다.")
        with st.expander(f"{label} 급수별 건수"):
            st.dataframe([{"급수": f"{level}급", "원본": word_snapshot.levels[level],
                           "대상 현재": word_plan.target_levels[level]} for level in range(3, 7)], hide_index=True)
        if effects["unchanged"] == len(word_snapshot.rows) and not effects["deleted"]:
            st.success("단어장이 로컬 원본과 동일합니다.")
        if include_questions:
            st.write("문제 테이블을 새로 만들고 등록합니다." if plan.create_table else "기존 문제를 로컬 유효 묶음과 동기화합니다.")
            question_effects = plan.effects
            st.dataframe([{"현재 문제": plan.target_count, "신규": question_effects["created"],
                           "변경": question_effects["updated"], "동일": question_effects["unchanged"],
                           "삭제": question_effects["deleted"]}], hide_index=True, width="stretch")
    ready = configured and not errors and len(plans) == len(selection)
    button_label = "선택 서버에 단어 문제 등록" if include_questions else "선택 서버에 단어장 등록"
    if st.button(button_label, type="primary", disabled=not ready):
        results = dict(st.session_state.get("vocabulary-transfer-results", {}))
        for label, plan in plans.items():
            with st.spinner(f"{label} 백업·등록·검증 중"):
                try:
                    execute = execute_question_transfer if include_questions else execute_vocabulary_transfer
                    results[label] = execute(source, targets[label], plan, ROOT)
                except TransferError as exc:
                    results[label] = {"status": "check_required", "message": str(exc)}
            st.session_state["vocabulary-transfer-results"] = results
        st.session_state.pop("vocabulary-transfer-preview", None)
        st.rerun()

results = st.session_state.get("vocabulary-transfer-results")
if results:
    st.subheader("최근 서버별 실행 결과")
    for label, result in results.items():
        if result["status"] == "verified":
            st.success(f"{label}: 원본 {result['source_count']:,}개 등록 검증 완료 · "
                       f"신규 {result['created']:,} · 변경 {result['updated']:,} · 동일 {result['unchanged']:,} · 삭제 {result['deleted']:,}")
            if include_questions:
                st.caption(f"전체 단어 {result['word_count']:,}개 · 문제 제외 단어 {result['excluded']:,}개 · "
                           f"대상 전체 문제 {result['target_count']:,}개 · 삭제 단어 {result['deleted_words']:,}개")
            else:
                st.caption(f"대상 전체 단어 {result['target_count']:,}개 · 함께 삭제한 연결 문제 {result['deleted_questions']:,}개")
            if result["backup"]:
                st.caption(f"변경 전 백업: {result['backup']}")
        else:
            st.warning(f"{label}: {result['message']}")
    st.caption("서버별로 독립 처리합니다. 재시도할 서버만 선택하고 ‘등록 내용 확인’을 다시 누르세요.")
