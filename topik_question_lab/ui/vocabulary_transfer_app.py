from pathlib import Path
import os

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.step_06_publication.vocabulary_transfer import (
    TransferError, capture_vocabulary, digest, execute_vocabulary_transfer, prepare_vocabulary_transfer,
)


ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="서버에 단어장 등록", page_icon="📡", layout="wide")
st.title("서버에 단어장 등록")
st.caption("생성기의 3~6급 전체 단어장을 선택 서버에 등록합니다. 원본 ID·뜻·예문을 보존합니다.")

source = os.getenv("DATABASE_URL", "").strip()
targets = {
    "로컬 서버": os.getenv("QUESTION_BANK_LOCAL_DATABASE_URL", "").strip(),
    "운영 서버": os.getenv("QUESTION_BANK_PRODUCTION_DATABASE_URL", "").strip(),
}
selection = st.multiselect("등록 대상", list(targets), default=["로컬 서버"])
config_key = digest((source, targets))
if st.session_state.get("vocabulary-transfer-config") != config_key:
    st.session_state["vocabulary-transfer-config"] = config_key
    st.session_state.pop("vocabulary-transfer-preview", None)
    st.session_state.pop("vocabulary-transfer-results", None)
configured = bool(source and selection and all(targets[label] for label in selection))
if not configured:
    st.info("DATABASE_URL과 선택 대상의 QUESTION_BANK_LOCAL_DATABASE_URL / QUESTION_BANK_PRODUCTION_DATABASE_URL을 설정하세요.")
if "운영 서버" in selection:
    st.warning("운영 서버가 선택되어 있습니다. 등록 버튼을 누르면 운영 DB에 적용됩니다.")
st.caption("대상에만 있는 단어는 보존합니다. 기존 단어장 변경 전에는 파일 백업을 만들고, ID 충돌은 등록을 중단합니다.")

request_key = digest((config_key, selection))
if st.button("등록 내용 확인", disabled=not configured):
    st.session_state.pop("vocabulary-transfer-preview", None)
    try:
        with st.spinner("원본과 대상 단어장을 확인합니다."):
            snapshot = capture_vocabulary(source)
            plans, errors, destinations = {}, {}, {}
            for label in selection:
                try:
                    plan = prepare_vocabulary_transfer(snapshot, targets[label])
                    identity = plan.target_identity
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
    st.subheader("생성기 원본")
    metrics = st.columns(5)
    metrics[0].metric("전체 단어", f"{len(snapshot.rows):,}")
    for column, level in zip(metrics[1:], range(3, 7)):
        column.metric(f"{level}급", f"{snapshot.levels[level]:,}")
    for label, message in errors.items():
        st.error(f"{label}: {message}")
    for label, plan in plans.items():
        effects = plan.effects
        st.subheader(label)
        st.write("단어장 테이블을 새로 만들고 등록합니다." if plan.create_table else "기존 단어장에 추가·갱신합니다.")
        if plan.upgrade_audio:
            st.info("기존 대상 단어장을 백업한 뒤 음성 저장 컬럼을 추가합니다.")
        if "audio_created" in effects:
            st.caption(f"음성 신규 연결 {effects['audio_created']:,}개 · 음성 변경 {effects['audio_updated']:,}개 · "
                       f"대상 음성 보존 {effects['audio_preserved']:,}개 · S3 파일은 공유합니다.")
        st.dataframe([{"현재 단어": plan.target_count, "신규": effects["created"], "변경": effects["updated"],
                       "동일": effects["unchanged"], "충돌": len(effects["conflicts"]),
                       "대상에만 있는 단어 (보존)": effects["extra"]}], hide_index=True, width="stretch")
        with st.expander(f"{label} 급수별 건수"):
            st.dataframe([{"급수": f"{level}급", "원본": snapshot.levels[level],
                           "대상 현재": plan.target_levels[level]} for level in range(3, 7)], hide_index=True)
        if effects["conflicts"]:
            st.error(f"ID·단어·품사 충돌 {len(effects['conflicts']):,}개를 해결한 후 다시 확인하세요.")
            st.dataframe(effects["conflicts"][:20], hide_index=True, width="stretch")
            if len(effects["conflicts"]) > 20:
                st.caption("충돌 목록은 앞의 20개를 표시합니다.")
        elif effects["unchanged"] == len(snapshot.rows):
            st.success("추가·갱신할 단어가 없습니다. 원본에 없는 대상 음성은 보존합니다.")
    ready = configured and not errors and len(plans) == len(selection) and all(not p.effects["conflicts"] for p in plans.values())
    if st.button("선택 서버에 단어장 등록", type="primary", disabled=not ready):
        results = dict(st.session_state.get("vocabulary-transfer-results", {}))
        for label, plan in plans.items():
            with st.spinner(f"{label} 백업·등록·검증 중"):
                try:
                    results[label] = execute_vocabulary_transfer(source, targets[label], plan, ROOT)
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
                       f"신규 {result['created']:,} · 변경 {result['updated']:,} · 동일 {result['unchanged']:,}")
            st.caption(f"대상 전체 {result['target_count']:,}개 · 대상에만 있는 단어 {result['extra']:,}개 보존")
            if result["backup"]:
                st.caption(f"변경 전 백업: {result['backup']}")
        else:
            st.warning(f"{label}: {result['message']}")
    st.caption("서버별로 독립 처리합니다. 재시도할 서버만 선택하고 ‘등록 내용 확인’을 다시 누르세요.")
