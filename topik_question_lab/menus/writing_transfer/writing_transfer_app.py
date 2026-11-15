import os

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.common.paths import application_root
from topik_question_lab.common.question_bank.connection import TransferError, digest
from topik_question_lab.common.ui.set_identity import korean_time
from topik_question_lab.menus.writing_transfer.writing_transfer import (
    capture_writing, execute_writing_transfer, list_source_sets, prepare_writing_transfer,
)


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="서버에 쓰기 문제 등록", page_icon="📡", layout="wide")
st.title("서버에 쓰기 문제 등록")
st.caption("Hub에 저장된 쓰기 세트를 선택 서버에 등록합니다. 같은 세트를 다시 등록하면 Hub의 최신 내용으로 갱신합니다.")

source = os.getenv("DATABASE_URL", "").strip()
targets = {
    "로컬 서버": os.getenv("QUESTION_BANK_LOCAL_DATABASE_URL", "").strip(),
    "운영 서버": os.getenv("QUESTION_BANK_PRODUCTION_DATABASE_URL", "").strip(),
}
selection = st.multiselect("등록 대상", list(targets), default=["로컬 서버"], key="writing-transfer-targets")
reset_table = "writing-transfer-table-rows" not in st.session_state
config_key = digest((source, targets))
if st.session_state.get("writing-transfer-config") != config_key:
    for key in ("catalog", "selected", "preview", "results"):
        st.session_state.pop("writing-transfer-" + key, None)
    st.session_state["writing-transfer-config"] = config_key
    reset_table = True
configured = bool(source and selection and all(targets[label] for label in selection))
if not configured:
    st.info("Hub와 선택 서버의 DB 연결 설정이 필요합니다. 기존 서버 등록 메뉴와 같은 연결 설정을 사용합니다.")

refresh = st.button("세트 목록 새로고침", disabled=not source, key="writing-transfer-refresh")
if source and (refresh or "writing-transfer-catalog" not in st.session_state):
    reset_table = True
    st.session_state.pop("writing-transfer-preview", None)
    st.session_state.pop("writing-transfer-results", None)
    try:
        st.session_state["writing-transfer-catalog"] = list_source_sets(source)
    except TransferError as exc:
        st.session_state["writing-transfer-catalog"] = []
        st.error(str(exc))
catalog = {row["set_id"]: row for row in st.session_state.get("writing-transfer-catalog", [])}
previous_selected = [sid for sid in st.session_state.get("writing-transfer-selected", []) if sid in catalog]
if reset_table:
    # Keep editor input stable between clicks; restore checkmarks by set ID on refresh.
    st.session_state["writing-transfer-table-rows"] = [
        {"선택": sid in previous_selected, "Hub 저장(KST)": korean_time(row["created_at"]),
         "최근 수정(KST)": korean_time(row["updated_at"]), "형식": row["exam_format"].upper(),
         "모델": row["model"], "문항 수": row["items"], "식별코드": sid[:8], "_set_id": sid}
        for sid, row in catalog.items()]
    st.session_state["writing-transfer-table-version"] = st.session_state.get("writing-transfer-table-version", 0) + 1
rows = st.session_state["writing-transfer-table-rows"]
st.subheader("등록할 쓰기 세트 선택")
st.caption("현재 PBT 51~54번의 완성된 세트를 등록할 수 있습니다. Hub에 저장한 시각은 한국 시간(KST)으로 표시합니다.")
selected = []
if rows:
    edited = st.data_editor(rows, hide_index=True, width="stretch", num_rows="fixed",
        disabled=[column for column in rows[0] if column != "선택"],
        column_config={"선택": st.column_config.CheckboxColumn("선택", required=True), "_set_id": None},
        key=f"writing-transfer-table-{st.session_state['writing-transfer-table-version']}")
    selected = [row["_set_id"] for row in edited if row["선택"] and row["_set_id"] in catalog]
else:
    st.info("등록할 세트가 없습니다. ‘TOPIK II 쓰기 PBT’의 ‘Hub 세트 저장’ 단계에서 먼저 저장하세요.")
if set(selected) != set(previous_selected):
    st.session_state.pop("writing-transfer-preview", None)
    st.session_state.pop("writing-transfer-results", None)
st.session_state["writing-transfer-selected"] = selected
st.caption(f"선택 {len(selected)}개 세트 · 총 {sum(catalog[sid]['items'] for sid in selected)}문항")
st.caption("변경 대상의 쓰기 데이터를 백업합니다. 선택한 세트와 문항의 ID는 유지하며, 다른 세트는 보존합니다.")
if "운영 서버" in selection:
    st.warning("운영 서버가 선택되어 있습니다. 등록 버튼을 누르면 운영 DB에 적용됩니다.")

request_key = digest((config_key, sorted(selection), sorted(selected)))
if st.button("등록 내용 확인", disabled=not configured or not selected, key="writing-transfer-preview-button"):
    st.session_state.pop("writing-transfer-preview", None)
    st.session_state.pop("writing-transfer-results", None)
    try:
        with st.spinner("Hub와 대상 쓰기 세트를 확인합니다."):
            snapshot = capture_writing(source, selected)
            plans, errors, destinations = {}, {}, {}
            for label in selection:
                try:
                    plan = prepare_writing_transfer(snapshot, targets[label])
                    identity = plan.target_identity
                    destination = digest((identity["cluster"] or (identity["address"], identity["port"]), identity["database"]))
                    if destination in destinations:
                        errors[label] = f"{destinations[destination]}와 같은 대상 DB입니다. 한 대상만 선택하세요."
                    else:
                        destinations[destination] = label
                        plans[label] = plan
                except TransferError as exc:
                    errors[label] = str(exc)
            st.session_state["writing-transfer-preview"] = (request_key, plans, errors)
    except TransferError as exc:
        st.error(str(exc))

preview = st.session_state.get("writing-transfer-preview")
if preview and preview[0] == request_key:
    _, plans, errors = preview
    for label, message in errors.items():
        st.error(f"{label}: {message}")
    for label, plan in plans.items():
        st.subheader(label)
        if plan.create_tables:
            st.info("등록 실행 시 이 서버에 쓰기 세트·문항 테이블 2개를 생성합니다.")
        st.dataframe([{"신규 세트": plan.effects["created"], "변경 세트": plan.effects["updated"],
                       "동일 세트": plan.effects["unchanged"], "등록 문항": len(plan.snapshot.rows["writing_questions"])}],
                     hide_index=True, width="stretch")
    ready = configured and not errors and len(plans) == len(selection)
    if st.button("선택 서버에 쓰기 세트 등록", type="primary", disabled=not ready, key="writing-transfer-execute"):
        results = {}
        for label, plan in plans.items():
            with st.spinner(f"{label} 백업·등록·검증 중"):
                try:
                    results[label] = execute_writing_transfer(source, targets[label], plan, ROOT)
                except TransferError as exc:
                    results[label] = {"status": "check_required", "message": str(exc)}
            st.session_state["writing-transfer-results"] = (request_key, results)
        st.session_state.pop("writing-transfer-preview", None)
        st.rerun()

result = st.session_state.get("writing-transfer-results")
if result and result[0] == request_key:
    for label, value in result[1].items():
        if value["status"] == "verified":
            st.success(f"{label}: 세트 {value['sets']}개 · 문항 {value['questions']}개 등록 검증 완료 · "
                       f"신규 {value['created']} · 변경 {value['updated']} · 동일 {value['unchanged']}")
            if value["backup"]:
                st.caption(f"변경 전 백업: {value['backup']}")
        else:
            st.warning(f"{label}: {value['message']}")
    st.caption("서버별로 독립 처리합니다. 재시도할 서버를 선택하고 ‘등록 내용 확인’을 다시 누르세요.")
