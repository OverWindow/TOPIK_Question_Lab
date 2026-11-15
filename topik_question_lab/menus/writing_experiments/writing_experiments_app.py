"""Browse saved writing experiments without starting model calls."""
import streamlit as st
from dotenv import load_dotenv

from topik_question_lab.common.paths import application_root
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing_experiments.writing_experiments_ui import render_manager


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="쓰기 실험 관리", page_icon="📊", layout="wide")
st.title("쓰기 실험 관리")
render_manager(WritingStorage(ROOT / "data/writing_pbt.db"))
