import os

import streamlit as st
from dotenv import load_dotenv

from topik_question_lab.common.paths import application_root
from topik_question_lab.common.ui.navigation import menu_page
from topik_question_lab.common.generation.runner_service import get_runner
from topik_question_lab.maintenance.startup import prepare_local_database
from topik_question_lab.maintenance.bank_migrations import StartupMigrationError
from topik_question_lab.common.question_bank.postgres_storage import MIGRATION_DIR


ROOT = application_root()
load_dotenv(ROOT / ".env")

try:
    revision = tuple((path.name, path.stat().st_mtime_ns) for path in sorted(MIGRATION_DIR.glob("*.sql")))
    prepare_local_database(str(ROOT), os.getenv("DATABASE_URL", "").strip(), revision)
except StartupMigrationError as exc:
    st.error(str(exc))
    if st.button("DB 준비 다시 시도"):
        st.rerun()
    st.stop()

navigation = st.navigation(
    {
        "TOPIK 문제 생성": [
            st.Page(menu_page("app.py"), title="TOPIK II 읽기", icon="📖", default=True),
            st.Page(menu_page("listening_app.py"), title="TOPIK II 듣기", icon="🎧"),
            st.Page(menu_page("bulk_generation_app.py"), title="문제 일괄 생성", icon="✨"),
            st.Page(menu_page("storage_overview_app.py"), title="문제 저장 현황", icon="📊"),
            st.Page(menu_page("bank_editor_app.py"), title="PostgreSQL 문제 관리", icon="✏️"),
            st.Page(menu_page("topic_bank_app.py"), title="소재 은행", icon="🗂️"),
            st.Page(menu_page("postgres_publish_app.py"), title="문제 등록·세트 만들기", icon="📤"),
            st.Page(menu_page("bank_transfer_app.py"), title="서버에 문제 등록", icon="📡"),
            st.Page(menu_page("backup_restore_app.py"), title="백업·복원", icon="💾"),
        ],
        "TOPIK 단어장": [
            st.Page(menu_page("vocabulary_app.py"), title="TOPIK II 단어장", icon="📚"),
            st.Page(menu_page("vocabulary_questions_app.py"), title="단어 연습문제 생성", icon="📝"),
            st.Page(menu_page("vocabulary_question_editor_app.py"), title="단어 문제 관리", icon="✏️"),
            st.Page(menu_page("vocabulary_transfer_app.py"), title="서버에 단어장·문제 등록", icon="📡"),
            st.Page(menu_page("korean_dictionary_app.py"), title="한국어 사전 등록", icon="📘"),
        ],
        "PostgreSQL 정리": [
            st.Page(menu_page("postgres_cleanup_app.py"), title="정리 작업", icon="🗄️"),
        ],
    },
    position="sidebar",
    expanded=True,
)
if navigation.title not in {"문제 저장 현황", "문제 등록·세트 만들기", "서버에 문제 등록", "PostgreSQL 문제 관리", "TOPIK II 단어장", "단어 연습문제 생성", "단어 문제 관리", "서버에 단어장·문제 등록", "한국어 사전 등록", "정리 작업"} and (ROOT / "data/bulk_generation.db").exists():
    get_runner(str(ROOT)).recover_idle()
navigation.run()
