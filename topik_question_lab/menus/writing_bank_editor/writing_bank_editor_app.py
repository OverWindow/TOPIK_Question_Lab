"""Browse Hub writing sets and edit their checked questions."""
import os

from dotenv import load_dotenv
from pydantic import ValidationError
import streamlit as st

from topik_question_lab.common.paths import application_root
from topik_question_lab.common.question_bank.connection import TransferError, digest
from topik_question_lab.common.question_bank.writing_postgres import WritingQuestionBank
from topik_question_lab.common.ui.set_identity import korean_time
from topik_question_lab.common.writing_models import PROFILES, WritingQuestion
from topik_question_lab.menus.writing.writing_pipeline import validation_message
from topik_question_lab.menus.writing.writing_ui import edit_question, render_question


def checked_rows(rows, key, hidden):
    edited = st.data_editor(
        rows, hide_index=True, width="stretch", num_rows="fixed",
        disabled=[column for column in rows[0] if column != "선택"],
        column_config={"선택": st.column_config.CheckboxColumn("선택", required=True), hidden: None},
        key=key,
    )
    return [row[hidden] for row in edited if row["선택"]]


def render_editor(bank, state, snapshot, record):
    sid, slot = record["set_id"], record["type_slot"]
    st.subheader(f"{slot}번 문항 수정 · {sid[:8]}")
    try:
        question = WritingQuestion.model_validate(record["content_json"])
    except ValidationError as exc:
        st.error(validation_message(exc))
        return
    draft = edit_question(question, f"hub-{state['config']}-{state['revision']}-{sid}-{slot}")
    try:
        preview = WritingQuestion.model_validate(draft)
    except ValidationError as exc:
        preview = None
        st.warning(validation_message(exc))
    if preview:
        with st.expander("수정 내용 미리보기", expanded=True):
            render_question(preview)
    if st.button("Hub PostgreSQL에 저장", key=f"writing-bank-save-{sid}-{slot}",
                 type="primary", disabled=preview is None):
        try:
            updated_at = bank.save_question(record, draft)
        except (ValidationError, ValueError) as exc:
            st.error(validation_message(exc) if isinstance(exc, ValidationError) else str(exc))
        else:
            record["content_json"] = preview.model_dump()
            snapshot["updated_at"] = updated_at
            next(row for row in state["catalog"] if row["set_id"] == sid)["updated_at"] = updated_at
            # Rebuild display rows while retaining checked IDs and other drafts.
            state["set_rows"] = None
            state["question_rows"].pop(sid, None)
            state["notice"] = f"{sid[:8]} 세트의 {slot}번 문항을 Hub PostgreSQL에 저장했습니다."
            return True
    return False


def render_page():
    load_dotenv(application_root() / ".env")
    st.set_page_config(page_title="쓰기 Hub PostgreSQL 관리", page_icon="✏️", layout="wide")
    st.title("쓰기 Hub PostgreSQL 관리")
    st.caption("세트를 체크한 뒤 문항을 체크하면 수정할 수 있습니다. 저장은 Hub에 반영됩니다.")
    st.caption("서버에도 반영하려면 ‘서버에 쓰기 문제 등록’ 메뉴에서 해당 세트를 다시 등록하세요.")
    url = os.getenv("DATABASE_URL", "").strip()
    config = digest(url)
    state = st.session_state.get("writing-bank-state")
    if state is None or state["config"] != config:
        state = {"config": config, "catalog": None, "snapshots": {}, "selected_sets": [],
                 "selected_questions": {}, "set_rows": None, "question_rows": {}, "revision": 0,
                 "set_table_version": 0, "question_table_versions": {}}
        st.session_state["writing-bank-state"] = state
    if not url:
        st.info("Hub PostgreSQL 연결 설정이 필요합니다. DATABASE_URL을 설정하세요.")
        return
    bank = WritingQuestionBank(url)
    refresh = st.button("목록 새로고침", key="writing-bank-refresh",
                        help="최신 데이터를 불러옵니다. 저장하지 않은 수정 내용은 초기화됩니다.")
    if refresh or state["catalog"] is None:
        state["revision"] += 1
        state["catalog"] = None
        state["snapshots"].clear()
        state["question_rows"].clear()
        state["set_rows"] = None
        try:
            if not bank.available():
                st.info("Hub에 쓰기 테이블이 없습니다. ‘TOPIK II 쓰기 PBT’에서 Hub 세트 저장을 먼저 준비하세요.")
                return
            state["catalog"] = bank.list_sets()
        except TransferError as exc:
            st.error(str(exc))
            return
    if notice := state.pop("notice", None):
        st.success(notice)
    catalog = {row["set_id"]: row for row in state["catalog"]}
    st.subheader("1. 쓰기 세트 선택")
    if not catalog:
        state["selected_sets"] = []
        state["selected_questions"].clear()
        st.info("저장된 쓰기 세트가 없습니다. ‘TOPIK II 쓰기 PBT’의 ‘Hub 세트 저장’ 단계에서 먼저 저장하세요.")
        return
    if state["set_rows"] is None:
        state["set_table_version"] += 1
        state["set_rows"] = [
            {"선택": sid in state["selected_sets"], "Hub 저장(KST)": korean_time(row["created_at"]),
             "최근 수정(KST)": korean_time(row["updated_at"]), "형식": row["exam_format"].upper(),
             "모델": row["model"], "문항 수": row["items"], "식별코드": sid[:8], "_set_id": sid}
            for sid, row in catalog.items()
        ]
    state["selected_sets"] = checked_rows(state["set_rows"],
        f"writing-bank-sets-{config}-{state['set_table_version']}", "_set_id")
    for sid in list(state["snapshots"]):
        if sid not in state["selected_sets"]:
            del state["snapshots"][sid]
            state["question_rows"].pop(sid, None)
    if not state["selected_sets"]:
        st.info("세트 표에서 조회할 세트를 체크하세요.")
        return
    saved = False
    for sid in state["selected_sets"]:
        st.subheader(f"2. 문항 선택 · {sid[:8]}")
        if sid not in state["snapshots"]:
            try:
                snapshot = bank.get_set(catalog[sid]["source_key"])
            except TransferError as exc:
                st.error(str(exc))
                continue
            if snapshot is None or snapshot["set_id"] != sid:
                st.warning("세트가 변경되었거나 삭제되었습니다. 목록을 새로고침하세요.")
                continue
            state["snapshots"][sid] = snapshot
        snapshot = state["snapshots"][sid]
        if snapshot["exam_format"] != "pbt":
            st.info("현재 PBT 쓰기 51~54번 문항만 수정할 수 있습니다.")
            continue
        questions = {row["type_slot"]: row for row in snapshot["questions"]}
        if not questions:
            st.info("이 세트에는 저장된 문항이 없습니다.")
            continue
        if sid not in state["question_rows"]:
            state["question_table_versions"][sid] = state["question_table_versions"].get(sid, 0) + 1
            state["question_rows"][sid] = [
                {"선택": slot in state["selected_questions"].get(sid, []), "문항 번호": slot,
                 "유형": PROFILES[slot].label, "소재": row["content_json"].get("topic", ""),
                 "지문·설명": row["content_json"].get("passage", "") or row["content_json"].get("data_source", ""),
                 "자료 수": len(row["content_json"].get("materials", [])), "_type_slot": slot}
                for slot, row in sorted(questions.items())
            ]
        selected = checked_rows(state["question_rows"][sid],
            f"writing-bank-questions-{config}-{sid}-{state['question_table_versions'][sid]}", "_type_slot")
        state["selected_questions"][sid] = selected
        if not selected:
            st.caption("문항 표에서 수정할 문항을 체크하세요.")
        for slot in selected:
            saved = render_editor(bank, state, snapshot, questions[slot]) or saved
    # Render every editor before rerunning so later widgets retain unsaved drafts.
    if saved:
        st.rerun()


render_page()
