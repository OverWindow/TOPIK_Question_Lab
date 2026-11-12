from topik_question_lab.common.paths import application_root
import os

from dotenv import load_dotenv
import streamlit as st
from topik_question_lab.menus.registration.registration_ui import render_app

ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="문제 등록·세트 만들기", page_icon="📤", layout="wide")
render_app(ROOT, os.getenv("DATABASE_URL", "").strip(), initial_mode="add")
