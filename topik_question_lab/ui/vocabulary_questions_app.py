from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.step_06_publication.vocabulary import PAGE_SIZE, search_vocabulary
from topik_question_lab.ui.vocabulary_questions_ui import render_progress, render_question_controls


ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="단어 연습문제 생성", page_icon="📝", layout="wide")
st.title("단어 연습문제 생성")
database_url = os.getenv("DATABASE_URL", "").strip()
render_progress(database_url)
st.caption("① 대상 선택 → ② 프롬프트 확인 → ③ 생성·검수·자동 저장 → ④ 실행 결과")
st.info("단어당 최대 7문제를 만듭니다. 1차 검수 통과 시 2회, 실패 시 수정·최종 검수를 포함해 최대 4회 호출합니다. "
        "먼저 단어 1개로 결과를 확인한 뒤 대량 생성하세요. "
        "통과한 단어는 즉시 PostgreSQL에 저장하고, 실패한 단어는 다음 생성에서 처음부터 시작합니다.")
st.subheader("1. 단어·생성 범위 선택")

filters = st.columns([1, 3])
level = filters[0].selectbox("급수", [None, 3, 4, 5, 6],
    format_func=lambda value: "전체" if value is None else f"{value}급", key="vocab-question-level")
query = filters[1].text_input("검색어", placeholder="단어, 뜻 또는 예문", max_chars=200, key="vocab-question-query")
st.button("새로고침", key="vocab-question-refresh")
identity = (hashlib.sha256(database_url.encode()).hexdigest(), level, query.strip())
if st.session_state.get("vocab-question-filter") != identity:
    st.session_state["vocab-question-filter"] = identity
    st.session_state["vocab-question-page"] = 1
    st.session_state.pop("vocab-question-word", None)

snapshot = search_vocabulary(database_url, level=level, query=query,
                             page=st.session_state.get("vocab-question-page", 1))
if not snapshot["available"]:
    st.warning(snapshot["message"])
    st.stop()

st.caption(f"검색 결과 {snapshot['total']:,}개 · 검색 결과 전체를 대량 생성 대상으로 선택할 수 있습니다.")
st.session_state["vocab-question-page"] = snapshot["page"]
rows = snapshot["rows"]
selected = None
if rows:
    def move_page(delta):
        st.session_state["vocab-question-page"] += delta

    previous, picker, following, status = st.columns([1, 1, 1, 4], vertical_alignment="bottom")
    previous.button("이전", disabled=snapshot["page"] == 1, on_click=move_page, args=(-1,), key="vocab-question-prev")
    picker.number_input("페이지", min_value=1, max_value=snapshot["pages"], step=1, key="vocab-question-page")
    following.button("다음", disabled=snapshot["page"] == snapshot["pages"], on_click=move_page, args=(1,), key="vocab-question-next")
    start = (snapshot["page"] - 1) * PAGE_SIZE + 1
    status.caption(f"{snapshot['page']} / {snapshot['pages']} 페이지 · {start:,}–{start + len(rows) - 1:,}번째 단어")
    by_id = {row["vocabulary_id"]: row for row in rows}
    if st.session_state.get("vocab-question-word") not in by_id:
        st.session_state.pop("vocab-question-word", None)
    selected_id = st.selectbox("생성할 단어", list(by_id), key="vocab-question-word",
        format_func=lambda value: f"{by_id[value]['korean']} · {by_id[value]['part_of_speech'] or '미지정'} · {by_id[value]['topik_level']}급")
    selected = by_id[selected_id]
    st.caption(f"뜻: {selected['english']} · 예문: {selected['example_korean']}")
else:
    st.info("조건에 맞는 단어가 없습니다. 급수나 검색어를 바꿔보세요.")
    st.session_state.pop("vocab-question-word", None)

render_question_controls(database_url, ROOT, selected, level=level, query=query)
