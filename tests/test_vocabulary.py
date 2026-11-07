from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.step_06_publication import vocabulary
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "topik_question_lab/ui/vocabulary_app.py"
SAMPLE = [
    ("사과", "명사", "Apple", "táo", "りんご", "사과를 먹어요.", "I eat an apple.", 3),
    ("환경", "명사", "environment", "môi trường", "環境", "지구를 보호해요.", "Protect the earth.", 4),
    ("경제", "", "economy", "kinh tế", "経済", "시장이 성장해요.", "The market grows.", 5),
    ("할인율", "명사", "100%_off!", "giảm", "割引率", "특별 할인 안내", "Special deal.", 6),
]


@pytest.fixture
def bank(postgres_database_factory, monkeypatch):
    url = postgres_database_factory()
    with psycopg.connect(url) as connection:
        connection.execute("CREATE SCHEMA topik_bank")
        connection.execute((MIGRATION_DIR / "012_vocabulary.sql").read_text(encoding="utf-8"))
        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO topik_bank.vocabulary
                   (korean, part_of_speech, english, vietnamese, japanese, example_korean, example_english, topik_level)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                SAMPLE + [(f"연습{i:03}", "명사", "practice", "luyện tập", "練習", "연습해요.", "I practice.", 3)
                          for i in range(51)],
            )
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_unconfigured_page_does_not_connect_or_create_files(tmp_path, monkeypatch):
    monkeypatch.setattr(vocabulary, "_psycopg", lambda: pytest.fail("must not connect"))
    before = list(tmp_path.rglob("*"))
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception
    assert app.title[0].value == "TOPIK II 단어장"
    assert "연결이 설정되지 않았습니다" in app.warning[0].value
    assert not app.dataframe and not app.metric
    assert list(tmp_path.rglob("*")) == before


def test_connection_error_is_sanitized(monkeypatch):
    class Driver:
        def connect(self, *args, **kwargs):
            assert "default_transaction_read_only=on" in kwargs["options"]
            raise RuntimeError("postgresql://user:synthetic-secret@localhost/test")

    monkeypatch.setattr(vocabulary, "_psycopg", lambda: (Driver(), None))
    monkeypatch.setenv("DATABASE_URL", "synthetic")
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception
    assert "새로고침" in app.warning[0].value
    assert "synthetic-secret" not in app.warning[0].value


def test_multilingual_search_combines_level_and_literal_text(bank):
    for query, expected in (("사과", "사과"), ("APPLE", "사과"), ("môi", "환경"), ("環境", "환경"),
                            ("지구", "환경"), ("EARTH", "환경"), ("%", "할인율"), ("_", "할인율"),
                            ("!", "할인율"), ("  사과  ", "사과")):
        result = vocabulary.search_vocabulary(bank, query=query)
        assert result["available"]
        assert [row["korean"] for row in result["rows"]] == [expected]
    assert vocabulary.search_vocabulary(bank, level=3, query="사과")["total"] == 1
    assert vocabulary.search_vocabulary(bank, level=4, query="사과")["total"] == 0
    assert vocabulary.search_vocabulary(bank, query="' OR true --")["total"] == 0
    assert vocabulary.search_vocabulary(bank, query="\\")["total"] == 0


def test_pagination_counts_and_out_of_range_pages(bank):
    first = vocabulary.search_vocabulary(bank)
    second = vocabulary.search_vocabulary(bank, page=2)
    assert first["total"] == 55 and first["pages"] == 2
    assert first["level_counts"] == {3: 52, 4: 1, 5: 1, 6: 1}
    assert len(first["rows"]) == 50 and len(second["rows"]) == 5
    assert not ({row["vocabulary_id"] for row in first["rows"]}
                & {row["vocabulary_id"] for row in second["rows"]})
    assert vocabulary.search_vocabulary(bank, page=999)["rows"] == second["rows"]
    for level, count in first["level_counts"].items():
        result = vocabulary.search_vocabulary(bank, level=level, page=999)
        assert result["total"] == count
        assert all(row["topik_level"] == level for row in result["rows"])
    empty = vocabulary.search_vocabulary(bank, query="nonexistent", page=2)
    assert empty["available"] and empty["rows"] == [] and empty["page"] == empty["pages"] == 1


def test_ui_filters_pages_details_and_refresh(bank):
    with psycopg.connect(bank) as connection:
        before = connection.execute("SELECT * FROM topik_bank.vocabulary ORDER BY vocabulary_id").fetchall()
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception
    assert app.metric[0].value == "55" and len(app.dataframe[0].value) == 50
    assert next(button for button in app.button if button.label == "이전").disabled
    next(button for button in app.button if button.label == "다음").click().run()
    assert not app.exception and app.number_input[0].value == 2
    assert len(app.dataframe[0].value) == 5
    assert next(button for button in app.button if button.label == "다음").disabled
    app.number_input[0].set_value(1).run()
    assert len(app.dataframe[0].value) == 50
    app.selectbox(key="vocabulary-level").set_value(5).run()
    assert not app.exception and app.number_input[0].value == 1
    assert list(app.dataframe[0].value["단어"]) == ["경제"]
    assert app.dataframe[0].value.iloc[0]["품사"] == "미지정"
    assert {element.value for element in app.text} == set(SAMPLE[2][2:7])
    app.text_input(key="vocabulary-query").set_value("no match").run()
    assert not app.exception and not app.dataframe
    assert any("조건에 맞는 단어" in info.value for info in app.info)
    assert not any(widget.label == "상세 보기" for widget in app.selectbox)
    app.selectbox(key="vocabulary-level").set_value(None)
    app.text_input(key="vocabulary-query").set_value("사과").run()
    assert not app.exception and app.subheader[0].value == "사과"
    assert {element.value for element in app.text} == set(SAMPLE[0][2:7])
    next(button for button in app.button if button.label == "새로고침").click().run()
    assert not app.exception and app.subheader[0].value == "사과"
    with psycopg.connect(bank) as connection:
        assert connection.execute("SELECT * FROM topik_bank.vocabulary ORDER BY vocabulary_id").fetchall() == before


def test_refresh_clamps_page_and_replaces_missing_selection(bank):
    app = AppTest.from_file(str(PAGE)).run()
    app.number_input[0].set_value(2).run()
    with psycopg.connect(bank) as connection:
        connection.execute("DELETE FROM topik_bank.vocabulary WHERE vocabulary_id > 4")
    next(button for button in app.button if button.label == "새로고침").click().run()
    assert not app.exception and app.number_input[0].value == 1
    assert len(app.dataframe[0].value) == 4
    picker = app.selectbox(key="vocabulary-detail")
    picker.set_value(3).run()
    assert not app.exception and app.subheader[0].value == "경제"


def test_empty_and_missing_table_do_not_create_schema(postgres_database_factory, monkeypatch):
    url = postgres_database_factory()
    monkeypatch.setenv("DATABASE_URL", url)
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception and "아직 준비되지 않았습니다" in app.warning[0].value
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT to_regclass('topik_bank.vocabulary')").fetchone()[0] is None
        connection.execute("CREATE SCHEMA topik_bank")
        connection.execute((MIGRATION_DIR / "012_vocabulary.sql").read_text(encoding="utf-8"))
    next(button for button in app.button if button.label == "새로고침").click().run()
    assert not app.exception and app.metric[0].value == "0"
    assert app.info[0].value == "등록된 단어가 없습니다."


def test_main_menu_opens_vocabulary_without_recovering_worker(tmp_path, monkeypatch):
    from topik_question_lab.ui import runner_service

    (tmp_path / "data").mkdir()
    (tmp_path / "data/bulk_generation.db").touch()
    monkeypatch.setattr(runner_service, "get_runner", lambda *a: pytest.fail("must not start a worker"))
    app = AppTest.from_file(str(ROOT / "topik_question_lab/main.py"))
    app.switch_page("ui/vocabulary_app.py").run()
    assert not app.exception
    assert app.title[0].value == "TOPIK II 단어장"
