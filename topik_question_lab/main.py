from pathlib import Path
import os

import streamlit as st
from dotenv import load_dotenv
from topik_question_lab.ui.runner_service import get_runner


APP_DIR = Path(__file__).resolve().parent / "ui"
ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(APP_DIR.parents[1]))).resolve()
load_dotenv(ROOT / ".env")

navigation = st.navigation(
    {
        "PBT 문제 생성": [
            st.Page(APP_DIR / "app.py", title="TOPIK II 읽기", icon="📖", default=True),
            st.Page(APP_DIR / "listening_app.py", title="TOPIK II 듣기", icon="🎧"),
            st.Page(APP_DIR / "bulk_generation_app.py", title="50문제 일괄 생성", icon="✨"),
            st.Page(APP_DIR / "storage_overview_app.py", title="문제 저장 현황", icon="📊"),
            st.Page(APP_DIR / "bank_editor_app.py", title="PostgreSQL 문제 관리", icon="✏️"),
            st.Page(APP_DIR / "topic_bank_app.py", title="소재 은행", icon="🗂️"),
            st.Page(APP_DIR / "postgres_publish_app.py", title="문제 등록·세트 만들기", icon="📤"),
            st.Page(APP_DIR / "bank_transfer_app.py", title="서버에 문제 등록", icon="📡"),
            st.Page(APP_DIR / "backup_restore_app.py", title="백업·복원", icon="💾"),
        ],
        "PostgreSQL 정리": [
            st.Page(APP_DIR / "postgres_cleanup_app.py", title="정리 작업", icon="🗄️"),
        ],
    },
    position="sidebar",
    expanded=True,
)
if navigation.title not in {"문제 저장 현황", "문제 등록·세트 만들기", "서버에 문제 등록", "PostgreSQL 문제 관리", "정리 작업"} and (ROOT / "data/bulk_generation.db").exists():
    get_runner(str(ROOT)).recover_idle()
navigation.run()
