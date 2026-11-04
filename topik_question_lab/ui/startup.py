"""Run database preparation once per app process and migration file revision."""
from pathlib import Path

import streamlit as st

from topik_question_lab.maintenance.bank_migrations import migrate_local_bank


@st.cache_resource(show_spinner="로컬 DB 구조 확인·백업·마이그레이션 중…")
def prepare_local_database(root: str, url: str, migration_revision: tuple):
    return migrate_local_bank(Path(root), url)
