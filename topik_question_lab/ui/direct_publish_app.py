from pathlib import Path
import os

from dotenv import load_dotenv
import streamlit as st
from topik_question_lab.ui.registration_ui import render_app

ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="문제 등록·세트 만들기", page_icon="📤", layout="wide")
render_app(ROOT, os.getenv("DATABASE_URL", "").strip(), initial_mode="batches")
