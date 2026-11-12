from __future__ import annotations

from topik_question_lab.common.paths import application_root

import hashlib
import os

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.common.vocabulary.vocabulary import PAGE_SIZE, search_vocabulary
from topik_question_lab.common.vocabulary.vocabulary_audio import audio_profile, audio_status
from topik_question_lab.menus.vocabulary.vocabulary_audio_ui import STATUS_LABELS, current_audio_settings, render_audio_controls, render_audio_results


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="TOPIK II 단어장", page_icon="📚", layout="wide")
st.title("TOPIK II 단어장")
st.caption("급수별 단어의 뜻과 예문을 확인하고 한국어 음성을 생성·재생하세요.")

filters = st.columns([1, 3, 1])
level = filters[0].selectbox("급수", [None, 3, 4, 5, 6],
                           format_func=lambda value: "전체" if value is None else f"{value}급",
                           key="vocabulary-level")
query = filters[1].text_input("검색어", placeholder="한국어 단어, 영어·베트남어·일본어·중국어·인도네시아어 뜻 또는 예문",
                             max_chars=200, key="vocabulary-query")
audio_filter = filters[2].selectbox("음성", ["all", "present", "missing", "stale"],
    format_func=lambda value: {"all": "전체", "present": "음성 있음", "missing": "음성 없음", "stale": "갱신 필요"}[value],
    key="vocabulary-audio-filter")
st.button("새로고침", key="vocabulary-refresh")
database_url = os.getenv("DATABASE_URL", "").strip()
settings = current_audio_settings()
speaker = st.session_state["vocabulary-audio-preferences"]["speaker"]
profile = audio_profile(settings, speaker)
database_key = hashlib.sha256(database_url.encode()).hexdigest()
if st.session_state.get("vocabulary-database") != database_key:
    st.session_state["vocabulary-database"] = database_key
    st.session_state.pop("vocabulary-audio-results", None)
identity = (database_key, level, query.strip(), audio_filter, profile if audio_filter == "stale" else None)
if st.session_state.get("vocabulary-filter") != identity:
    st.session_state["vocabulary-filter"] = identity
    st.session_state["vocabulary-page"] = 1
    st.session_state.pop("vocabulary-detail", None)

snapshot = search_vocabulary(database_url, level=level, query=query,
                             page=st.session_state.get("vocabulary-page", 1),
                             audio_filter=audio_filter, audio_profile=profile)
if not snapshot["available"]:
    st.warning(snapshot["message"])
    st.stop()

counts = snapshot["level_counts"]
metrics = st.columns(5)
metrics[0].metric("전체 단어", f"{sum(counts.values()):,}")
for column, grade in zip(metrics[1:], range(3, 7)):
    column.metric(f"{grade}급", f"{counts[grade]:,}")
st.caption(f"검색 결과 {snapshot['total']:,}개 · 단어·뜻·예문의 부분 검색 · 영어 대소문자 구분 없음")
st.caption(f"음성 등록 {snapshot['audio_count']:,} / {sum(counts.values()):,}개")
st.session_state["vocabulary-page"] = snapshot["page"]
rows = snapshot["rows"]
if not rows:
    st.info("등록된 단어가 없습니다." if not sum(counts.values())
            else "조건에 맞는 단어가 없습니다. 급수나 검색어를 바꿔보세요.")
    st.session_state.pop("vocabulary-detail", None)
    render_audio_results()
    st.stop()


def move_page(delta):
    st.session_state["vocabulary-page"] += delta


previous, page_picker, following, status = st.columns([1, 1, 1, 4], vertical_alignment="bottom")
previous.button("이전", disabled=snapshot["page"] == 1, on_click=move_page, args=(-1,))
page_picker.number_input("페이지", min_value=1, max_value=snapshot["pages"], step=1, key="vocabulary-page")
following.button("다음", disabled=snapshot["page"] == snapshot["pages"], on_click=move_page, args=(1,))
start = (snapshot["page"] - 1) * PAGE_SIZE + 1
status.caption(f"{snapshot['page']} / {snapshot['pages']} 페이지 · {start:,}–{start + len(rows) - 1:,}번째 단어")
st.dataframe([{
    "급수": f"{row['topik_level']}급", "단어": row["korean"], "품사": row["part_of_speech"] or "미지정",
    "영어": row["english"], "베트남어": row["vietnamese"], "일본어": row["japanese"],
    "중국어(간체)": row.get("chinese_simplified", "") or "미등록",
    "인도네시아어": row.get("indonesian", "") or "미등록",
    "음성": STATUS_LABELS[audio_status(row, profile)],
} for row in rows], hide_index=True, width="stretch", height=420)

by_id = {row["vocabulary_id"]: row for row in rows}
if st.session_state.get("vocabulary-detail") not in by_id:
    st.session_state.pop("vocabulary-detail", None)
selected_id = st.selectbox("상세 보기", list(by_id), key="vocabulary-detail",
                           format_func=lambda key: f"{by_id[key]['korean']} · {by_id[key]['part_of_speech'] or '미지정'} · {by_id[key]['topik_level']}급")
selected = by_id[selected_id]
st.subheader(selected["korean"])
st.caption(f"{selected['topik_level']}급 · 품사: {selected['part_of_speech'] or '미지정'}")
meanings, examples = st.columns(2)
for field, label in (("english", "영어"), ("vietnamese", "베트남어"), ("japanese", "일본어"),
                     ("chinese_simplified", "중국어(간체)"), ("indonesian", "인도네시아어")):
    meanings.markdown(f"**{label}**")
    if selected.get(field):
        meanings.text(selected[field])
    else:
        meanings.caption("미등록")
for field, label in (("example_korean", "한국어 예문"), ("example_english", "영어 예문")):
    examples.markdown(f"**{label}**")
    examples.text(selected[field])

render_audio_controls(database_url, ROOT, snapshot, selected, level=level, query=query,
                      settings=settings, speaker=speaker)
