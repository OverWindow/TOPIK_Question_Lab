from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.ui.navigation import menu_page


PAGES = [
    ("app.py", "기출 데이터"),
    ("listening_app.py", "기출 데이터"),
    ("bulk_generation_app.py", "문제 일괄 생성"),
    ("storage_overview_app.py", "문제 저장 현황"),
    ("bank_editor_app.py", "PostgreSQL 문제 관리"),
    ("topic_bank_app.py", "소재 은행"),
    ("postgres_publish_app.py", "문제 등록·세트 만들기"),
    ("bank_transfer_app.py", "서버에 문제 등록"),
    ("backup_restore_app.py", "백업·복원"),
    ("vocabulary_app.py", "TOPIK II 단어장"),
    ("vocabulary_questions_app.py", "단어 연습문제 생성"),
    ("vocabulary_question_editor_app.py", "단어 문제 관리"),
    ("vocabulary_transfer_app.py", "서버에 단어장·문제 등록"),
    ("korean_dictionary_app.py", "한국어 사전 등록"),
    ("postgres_cleanup_app.py", "PostgreSQL 정리"),
]


@pytest.mark.parametrize("filename,title", PAGES)
def test_main_routes_to_each_menu_with_existing_urls(filename, title, monkeypatch):
    original = st.navigation
    urls = []

    def navigation(pages, **kwargs):
        urls.extend(page.url_path for group in pages.values() for page in group)
        return original(pages, **kwargs)

    monkeypatch.setattr(st, "navigation", navigation)
    app = AppTest.from_file(str(PACKAGE_ROOT / "main.py"), default_timeout=30)
    app.switch_page(menu_page(filename).relative_to(PACKAGE_ROOT).as_posix()).run()

    assert not app.exception
    assert any(title in heading.value for heading in app.title)
    assert urls == ["" if name == "app.py" else Path(name).stem for name, _ in PAGES]


def test_registration_button_opens_storage_overview():
    app = AppTest.from_file(str(PACKAGE_ROOT / "main.py"), default_timeout=30)
    app.switch_page(menu_page("postgres_publish_app.py").relative_to(PACKAGE_ROOT).as_posix()).run()
    button = next(button for button in app.button if button.label == "문제 저장 현황 열기")
    button.click().run()
    assert not app.exception
    assert app.title[0].value == "문제 저장 현황"
