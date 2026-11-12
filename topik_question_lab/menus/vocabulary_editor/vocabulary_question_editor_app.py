"""Search, edit, and delete word exercises already stored in PostgreSQL."""

from topik_question_lab.common.paths import application_root
import os

from dotenv import load_dotenv
from pydantic import ValidationError
import streamlit as st

from topik_question_lab.common.vocabulary.vocabulary_models import digest
from topik_question_lab.menus.vocabulary_editor.vocabulary_question_editor import TYPES, delete_question, load_question, search_questions, selected_question, update_question


ROOT = application_root()
load_dotenv(ROOT / ".env")
PREFIX = "vocab-manager"


def clear_selection():
    st.session_state.pop(f"{PREFIX}-selected", None)
    st.session_state.pop(f"{PREFIX}-snapshot", None)


def move_page(delta):
    st.session_state[f"{PREFIX}-page"] += delta
    clear_selection()


def finish(message):
    st.session_state.pop(f"{PREFIX}-snapshot", None)
    st.session_state[f"{PREFIX}-message"] = message


def save_changes(url, snapshot, key):
    row = selected_question(snapshot)
    payload = {"question_type": row["question_type"], "position": row["position"],
               **{field: st.session_state[f"{key}-{field}"] for field in ("instruction", "stem", "explanation")},
               "choices": [st.session_state[f"{key}-choice-{i}"] for i in range(4)],
               "correct_answer": st.session_state[f"{key}-answer"],
               "accepted_answers": st.session_state[f"{key}-accepted"].splitlines()
                   if row["question_type"] == "sentence_blank" else []}
    try:
        update_question(url, snapshot, payload)
    except ValidationError as exc:
        labels = {"instruction": "질문·지시문", "stem": "문제 문장", "choices": "보기",
                  "correct_answer": "정답 번호", "accepted_answers": "허용 답안", "explanation": "해설"}
        errors = []
        for error in exc.errors(include_input=False):
            field = labels.get(error["loc"][0], "문항") if error["loc"] else "문항"
            errors.append(f"{field}: {error['msg']}")
        st.session_state[f"{PREFIX}-errors"] = errors
    except ValueError as exc:
        st.session_state[f"{PREFIX}-errors"] = [str(exc)]
    else:
        finish(f"‘{snapshot['word']['korean']}’ {row['position']}번 문항을 PostgreSQL에 저장했습니다.")


def remove_selected(url, snapshot):
    try:
        delete_question(url, snapshot)
    except ValueError as exc:
        st.session_state[f"{PREFIX}-errors"] = [str(exc)]
    else:
        clear_selection()
        finish(f"‘{snapshot['word']['korean']}’ {selected_question(snapshot)['position']}번 문항을 삭제했습니다.")


def render_editor(url, snapshot):
    row, word = selected_question(snapshot), snapshot["word"]
    key = f"{PREFIX}-edit-{digest(snapshot)[:16]}"
    st.subheader(f"{word['korean']} · {row['position']}번 {TYPES[row['question_type']]}")
    st.caption(f"문항 ID {row['question_id']} · {word['topik_level']}급 · {word['part_of_speech']} · 뜻: {word['english']}")
    st.caption("내용을 수정한 뒤 ‘PostgreSQL에 저장’을 누르면 반영됩니다. 문항 유형과 순서는 유지됩니다.")
    with st.form(key):
        st.text_area("질문·지시문", row["instruction"], key=f"{key}-instruction")
        st.text_area("문제 문장", row["stem"], height=120, key=f"{key}-stem")
        columns = st.columns(2)
        for i in range(4):
            columns[i % 2].text_input(f"보기 {i + 1}", row["choices"][i], key=f"{key}-choice-{i}")
        st.selectbox("정답 번호", [1, 2, 3, 4], index=row["correct_answer"] - 1, key=f"{key}-answer")
        if row["question_type"] == "sentence_blank":
            st.text_area("주관식 허용 답안 (한 줄에 하나)", "\n".join(row["accepted_answers"]),
                         help="정답 보기를 반드시 포함하고, 오답 보기는 포함하지 마세요.", key=f"{key}-accepted")
        st.text_area("해설", row["explanation"], height=130, key=f"{key}-explanation")
        st.form_submit_button("PostgreSQL에 저장", type="primary", on_click=save_changes, args=(url, snapshot, key))

    with st.expander("선택 문항 삭제"):
        st.warning(f"‘{word['korean']}’ {row['position']}번 문항(ID {row['question_id']}) 1개를 영구 삭제합니다. "
                   "단어장 원본과 나머지 문항은 유지됩니다.")
        st.caption("필수 문항을 삭제하면 해당 단어가 생성 메뉴에서 ‘갱신 필요’로 표시될 수 있습니다.")
        confirmed = st.checkbox("선택한 문항 1개를 영구 삭제하는 데 동의합니다.", key=f"{key}-confirm")
        st.button("선택 문항 영구 삭제", disabled=not confirmed, key=f"{key}-delete",
                  on_click=remove_selected, args=(url, snapshot))


st.set_page_config(page_title="단어 문제 관리", page_icon="✏️", layout="wide")
st.title("단어 문제 관리")
st.caption("PostgreSQL에 저장된 단어 문제를 검색하고 수정·삭제합니다.")
message = st.session_state.pop(f"{PREFIX}-message", None)
if message:
    st.success(message)
for error in st.session_state.pop(f"{PREFIX}-errors", []):
    st.error(error)

filters = st.columns([3, 1, 1])
query = filters[0].text_input("검색어", placeholder="단어, 뜻, 문제 문장, 보기, 해설 또는 문항 ID",
                              max_chars=200, key=f"{PREFIX}-query")
level = filters[1].selectbox("급수", [None, 3, 4, 5, 6],
                             format_func=lambda v: "전체" if v is None else f"{v}급", key=f"{PREFIX}-level")
question_type = filters[2].selectbox("문제 유형", [None, *TYPES],
                                     format_func=lambda v: TYPES.get(v, "전체"), key=f"{PREFIX}-type")
st.button("새로고침", on_click=clear_selection, key=f"{PREFIX}-refresh")
url = os.getenv("DATABASE_URL", "").strip()
identity = (digest(url), query.strip(), level, question_type)
if st.session_state.get(f"{PREFIX}-filter") != identity:
    st.session_state[f"{PREFIX}-filter"] = identity
    st.session_state[f"{PREFIX}-page"] = 1
    clear_selection()

try:
    results = search_questions(url, query=query, level=level, question_type=question_type,
                               page=st.session_state.get(f"{PREFIX}-page", 1))
except ValueError as exc:
    st.error(str(exc))
    st.stop()
if not results["available"]:
    st.warning(results["message"])
    st.stop()
st.caption(f"검색 결과 {results['total']:,}개 문항")
if not results["rows"]:
    clear_selection()
    st.info("조건에 맞는 저장된 문항이 없습니다. 검색어·급수·문제 유형을 확인하세요.")
    st.stop()

st.session_state[f"{PREFIX}-page"] = results["page"]
previous, picker, following, status = st.columns([1, 1, 1, 4], vertical_alignment="bottom")
previous.button("이전", disabled=results["page"] == 1, on_click=move_page, args=(-1,), key=f"{PREFIX}-prev")
picker.number_input("페이지", min_value=1, max_value=results["pages"], step=1,
                    on_change=clear_selection, key=f"{PREFIX}-page")
following.button("다음", disabled=results["page"] == results["pages"], on_click=move_page, args=(1,), key=f"{PREFIX}-next")
status.caption(f"{results['page']} / {results['pages']} 페이지")
st.dataframe([{"문항 ID": row["question_id"], "단어": row["korean"], "급수": row["topik_level"],
               "유형": TYPES[row["question_type"]], "순서": row["position"], "문제 문장": row["stem"]}
              for row in results["rows"]], hide_index=True, width="stretch")
by_id = {row["question_id"]: row for row in results["rows"]}
if st.session_state.get(f"{PREFIX}-selected") not in by_id:
    clear_selection()
selected_id = st.selectbox("수정·삭제할 문항", list(by_id), key=f"{PREFIX}-selected",
    format_func=lambda v: f"{by_id[v]['korean']} · {by_id[v]['position']}번 · {TYPES[by_id[v]['question_type']]} · ID {v}")
snapshot = st.session_state.get(f"{PREFIX}-snapshot")
try:
    # Retain the originally displayed version across form submissions for conflict detection.
    if snapshot is None or snapshot["question_id"] != selected_id:
        snapshot = load_question(url, selected_id)
        st.session_state[f"{PREFIX}-snapshot"] = snapshot
except ValueError as exc:
    st.error(str(exc))
    st.stop()
render_editor(url, snapshot)
