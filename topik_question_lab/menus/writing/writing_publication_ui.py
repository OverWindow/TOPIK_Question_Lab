"""Select one question per writing type and save a Hub set."""
import sqlite3

import streamlit as st
from pydantic import ValidationError

from topik_question_lab.common.question_bank.writing_postgres import WritingQuestionBank, validate_writing_set
from topik_question_lab.common.question_bank.connection import TransferError
from topik_question_lab.menus.writing.writing_pipeline import validation_message
from topik_question_lab.menus.writing.writing_ui import render_question


def render_publication(storage, database_url):
    st.subheader("Hub에 쓰기 세트 저장")
    st.caption("51~54번을 각각 골라 한 세트로 저장합니다. ‘5. 수정·내보내기’에서 저장한 수정본을 사용합니다.")
    records = storage.list_generated()
    if not records:
        st.info("먼저 ‘4. 문제 생성’에서 유형별로 한 문항씩 생성하세요.")
        return
    selected_ids = []
    for slot in (51, 52, 53, 54):
        candidates = {row["id"]: row for row in records if row["question"]["type_slot"] == slot}
        if not candidates:
            st.warning(f"{slot}번 문항이 없습니다. 해당 유형을 먼저 생성하세요.")
            return
        selected_ids.append(st.selectbox(
            f"{slot}번 문항", list(candidates), key=f"writing-publish-{slot}",
            format_func=lambda value, rows=candidates: f"#{value} · {rows[value]['question']['topic']} · 실행 #{rows[value]['run_id']} · {rows[value]['model']}",
        ))
    selected = [row for row in records if row["id"] in selected_ids]
    try:
        questions = validate_writing_set(selected)
    except ValueError as exc:
        st.warning(validation_message(exc) if isinstance(exc, ValidationError) else str(exc))
        return
    for _, question in questions:
        with st.expander(f"{question.type_slot}번 · {question.topic}"):
            render_question(question)
    if not database_url:
        st.info("Hub PostgreSQL 연결 설정이 필요합니다.")
        return
    bank = WritingQuestionBank(database_url)
    try:
        if not bank.available():
            st.warning("쓰기 저장 테이블이 준비되지 않았습니다. 앱을 다시 시작해 DB 준비를 완료하세요.")
            return
        source_key = storage.selection_source_key(selected_ids)
        saved = bank.get_set(source_key) if source_key else None
        if saved:
            st.caption(f"Hub 저장 이력: {saved['set_id']} · 다시 저장하면 같은 세트에 수정 내용을 반영합니다.")
        if st.button("Hub에 세트 저장", type="primary", key="writing-publish"):
            # Reload at the write boundary; an old preview must not replace newer edits.
            current = [row for row in storage.list_generated() if row["id"] in selected_ids]
            validate_writing_set(current)
            receipt = bank.save_set(storage.selection_source_key(selected_ids, create=True), current)
            action = "저장" if receipt["created"] else "갱신"
            st.success(f"쓰기 4문항을 한 세트로 {action}했습니다. 세트 ID: {receipt['set_id']}")
    except ValueError as exc:
        st.error(validation_message(exc) if isinstance(exc, ValidationError) else str(exc))
    except sqlite3.Error:
        st.error("로컬 쓰기 저장 기록을 읽거나 갱신하지 못했습니다. 다시 시도하세요.")
    except TransferError as exc:
        st.error(str(exc))
