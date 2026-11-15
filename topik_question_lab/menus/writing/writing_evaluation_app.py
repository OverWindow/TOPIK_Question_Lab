"""Independent writing evaluation workshop page."""
from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.common.paths import application_root
from topik_question_lab.common.writing_models import PROFILES
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.common import writing_experiments as lab
from topik_question_lab.menus.writing.writing_evaluation_ui import render_evaluation_lab
from topik_question_lab.menus.writing.writing_pipeline import load_references
from topik_question_lab.menus.writing.writing_ui import pdf_page
from topik_question_lab.menus.writing.writing_experiment_ui import render_bulk_experiments


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="평가 프롬프트 실험실", page_icon="🧪", layout="wide")
st.title("평가 프롬프트 실험실")
storage = WritingStorage(ROOT / "data/writing_pbt.db")
references = load_references()
opening = st.session_state.pop("writing-experiment-open", None)
if opening:
    try:
        experiment = lab.load_experiment(storage, opening["id"])
        if experiment["source_key"] not in {r.source_key for r in references}:
            raise ValueError("이 실험의 기출 문항을 현재 실험실에서 찾을 수 없습니다.")
    except ValueError as exc:
        st.error(str(exc))
    else:
        target_slot = experiment["reference"]["question"]["type_slot"]
        st.session_state["writing-evaluation-type"] = target_slot
        st.session_state["writing-evaluation-mode"] = "대량 실험"
        st.session_state[f"experiment-source-{target_slot}"] = experiment["source_key"]
        st.session_state[f"experiment-selected-{experiment['source_key']}"] = experiment["id"]
        rounds = experiment["rounds"]
        if rounds:
            target_round = opening.get("round_id")
            if target_round not in {r["id"] for r in rounds}:
                target_round = next((r["id"] for r in rounds if any(t["status"] != "succeeded" for t in r["tasks"])), rounds[-1]["id"])
            st.session_state[f"experiment-round-{experiment['id']}"] = target_round
slot = st.sidebar.selectbox("평가할 쓰기 유형", list(PROFILES), format_func=lambda value: PROFILES[value].label,
                            key="writing-evaluation-type")
st.sidebar.caption("51·52번 각 10점 / 53번 30점 / 54번 50점")

mode = st.radio("실험 방식", ["개별 평가", "대량 실험"], horizontal=True, key="writing-evaluation-mode")
if mode == "대량 실험":
    render_bulk_experiments(storage, references, slot)
else:
    render_evaluation_lab(storage, references, slot, pdf_renderer=pdf_page)
