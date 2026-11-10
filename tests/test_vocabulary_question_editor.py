from copy import deepcopy
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from test_vocabulary_questions import CONFIG, bank, ready_task
from topik_question_lab.step_06_publication import vocabulary_question_editor as editor
from topik_question_lab.step_06_publication.vocabulary_questions import QUESTION_FIELDS, question_status


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "topik_question_lab/ui/vocabulary_question_editor_app.py"


@pytest.fixture
def saved_bank(bank):
    bank.save(ready_task(bank), CONFIG, bank.identity())
    return bank


def first_snapshot(bank):
    question_id = bank.snapshot(1)["existing"][0]["question_id"]
    return editor.load_question(bank.url, question_id)


def payload(snapshot, **changes):
    row = editor.selected_question(snapshot)
    return {**{key: deepcopy(row[key]) for key in QUESTION_FIELDS}, **changes}


def widget(app, kind, suffix):
    return next(item for item in getattr(app, kind) if item.key and item.key.endswith(suffix))


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_page_without_database_or_worker(tmp_path, monkeypatch):
    from topik_question_lab.ui import runner_service
    (tmp_path / "data").mkdir()
    (tmp_path / "data/bulk_generation.db").touch()
    monkeypatch.setattr(runner_service, "get_runner", lambda *a: pytest.fail("must not start a worker"))
    app = AppTest.from_file(str(ROOT / "topik_question_lab/main.py"))
    app.switch_page("ui/vocabulary_question_editor_app.py").run()
    assert not app.exception and app.title[0].value == "단어 문제 관리"
    assert "설정되지 않았습니다" in app.warning[0].value
    assert list(tmp_path.rglob("*.db")) == [tmp_path / "data/bulk_generation.db"]


def test_connection_error_is_redacted(monkeypatch):
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("postgresql://user:synthetic-secret@localhost/test")
    monkeypatch.setenv("DATABASE_URL", "configured")
    monkeypatch.setattr(psycopg, "connect", fail)
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception and app.error
    assert "synthetic-secret" not in app.error[0].value


def test_missing_schema_is_read_only(postgres_database_factory, monkeypatch):
    url = postgres_database_factory()
    monkeypatch.setenv("DATABASE_URL", url)
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception and "아직 준비되지" in app.warning[0].value
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_regnamespace('topik_bank')").fetchone()[0] is None


@pytest.mark.parametrize("query", ["미루", "POSTPONE", "hoãn", "延期", "운동회", "지켰어요", "미루었어요", "예정된"])
def test_search_word_translations_question_choices_and_explanation(saved_bank, query):
    result = editor.search_questions(saved_bank.url, query=query)
    assert result["available"] and result["total"] > 0
    assert all(row["korean"] == "미루다" for row in result["rows"])


def test_search_filters_and_literal_wildcards(saved_bank):
    assert editor.search_questions(saved_bank.url, level=4)["total"] == 0
    for kind, count in (("sentence_blank", 3), ("conjugation", 3), ("synonym", 1), ("antonym", 0)):
        result = editor.search_questions(saved_bank.url, level=3, question_type=kind)
        assert result["total"] == count
    snapshot = first_snapshot(saved_bank)
    editor.update_question(saved_bank.url, snapshot, payload(snapshot, explanation="literal 50%_! marker"))
    for query in ("%", "_", "!", "50%_!"):
        assert editor.search_questions(saved_bank.url, query=query)["total"] == 1
    assert editor.search_questions(saved_bank.url, query="' OR TRUE --")["total"] == 0


def test_pagination_and_refresh_after_last_page_disappears(saved_bank, monkeypatch):
    fields = [key for key in saved_bank.snapshot(1)["existing"][0] if key != "question_id"]
    with psycopg.connect(saved_bank.url) as c:
        for number in range(8):
            word_id = c.execute("""INSERT INTO topik_bank.vocabulary(korean,part_of_speech,english,vietnamese,
                japanese,example_korean,example_english,topik_level)
                SELECT %s,part_of_speech,english,vietnamese,japanese,example_korean,example_english,topik_level
                FROM topik_bank.vocabulary WHERE vocabulary_id=1 RETURNING vocabulary_id""",
                (f"추가단어{number}",)).fetchone()[0]
            columns = ",".join("%s" if key == "vocabulary_id" else key for key in fields)
            c.execute(f"INSERT INTO topik_bank.vocabulary_questions({','.join(fields)}) "
                      f"SELECT {columns} FROM topik_bank.vocabulary_questions WHERE vocabulary_id=1", (word_id,))
    first = editor.search_questions(saved_bank.url)
    last = editor.search_questions(saved_bank.url, page=99)
    assert first["total"] == 63 and len(first["rows"]) == 50
    assert last["page"] == last["pages"] == 2 and len(last["rows"]) == 13
    assert not {q["question_id"] for q in first["rows"]} & {q["question_id"] for q in last["rows"]}
    monkeypatch.setenv("DATABASE_URL", saved_bank.url)
    app = AppTest.from_file(str(PAGE)).run()
    button(app, "다음").click().run()
    assert app.number_input(key="vocab-manager-page").value == 2
    with psycopg.connect(saved_bank.url) as c:
        c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE vocabulary_id<>1")
    button(app, "새로고침").click().run()
    assert not app.exception and app.number_input(key="vocab-manager-page").value == 1
    assert len(app.dataframe[0].value) == 7


def test_edit_preserves_ids_provenance_word_and_other_questions(saved_bank):
    snapshot = first_snapshot(saved_bank)
    question = payload(snapshot, instruction="가장 알맞은 답을 고르세요.", stem="행사를 다음 주로 ( ).",
                       choices=["미뤘습니다", "지켰습니다", "잊었습니다", "시작했습니다"], correct_answer=1,
                       accepted_answers=["미뤘습니다", "미루었습니다"], explanation="행사 일정을 뒤로 변경했습니다.")
    editor.update_question(saved_bank.url, snapshot, question)
    after = saved_bank.snapshot(1)
    assert after["word"] == snapshot["word"]
    assert after["existing"][1:] == snapshot["existing"][1:]
    row = after["existing"][0]
    assert {key: row[key] for key in QUESTION_FIELDS} == question
    for key in ("question_id", "vocabulary_id", "provider", "model", "generation_id", "source_hash", "created_at"):
        assert row[key] == snapshot["existing"][0][key]
    assert row["updated_at"] != snapshot["existing"][0]["updated_at"]


@pytest.mark.parametrize("changes", [
    {"choices": ["같음"] * 4}, {"choices": ["", "a", "b", "c"]}, {"correct_answer": 5},
    {"accepted_answers": []}, {"accepted_answers": ["미뤘어요", "지켰어요"]},
    {"stem": "빈칸 없음"}, {"instruction": " "}, {"explanation": ""},
    {"position": 2}, {"question_type": "conjugation", "position": 4, "accepted_answers": []},
])
def test_invalid_edit_does_not_write(saved_bank, changes):
    snapshot = first_snapshot(saved_bank)
    with pytest.raises(ValueError):
        editor.update_question(saved_bank.url, snapshot, payload(snapshot, **changes))
    assert saved_bank.snapshot(1)["existing"] == snapshot["existing"]


@pytest.mark.parametrize("action", ["update", "delete"])
@pytest.mark.parametrize("change", ["word", "question", "deleted", "database"])
def test_stale_edit_or_delete_is_rejected(saved_bank, action, change):
    snapshot = first_snapshot(saved_bank)
    with psycopg.connect(saved_bank.url) as c:
        if change == "word":
            c.execute("UPDATE topik_bank.vocabulary SET english='defer' WHERE vocabulary_id=1")
        elif change == "question":
            c.execute("UPDATE topik_bank.vocabulary_questions SET explanation='다른 사용자의 수정' WHERE position=2")
        elif change == "deleted":
            c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE position=1")
        else:
            snapshot["database_key"] = "different database"
    before = saved_bank.snapshot(1)
    with pytest.raises(ValueError, match="변경|DB"):
        if action == "update":
            editor.update_question(saved_bank.url, snapshot, payload(snapshot, explanation="덮어쓰기 시도"))
        else:
            editor.delete_question(saved_bank.url, snapshot)
    assert saved_bank.snapshot(1) == before


@pytest.mark.parametrize("action", ["update", "delete"])
def test_existing_generation_cannot_overwrite_manual_changes(saved_bank, action):
    task = ready_task(saved_bank)
    snapshot = first_snapshot(saved_bank)
    if action == "update":
        editor.update_question(saved_bank.url, snapshot, payload(snapshot, explanation="사용자가 직접 수정한 해설"))
    else:
        editor.delete_question(saved_bank.url, snapshot)
    with pytest.raises(ValueError, match="기존 문항"):
        saved_bank.save(task, CONFIG, saved_bank.identity())


def test_delete_only_selected_question_and_keep_word(saved_bank):
    snapshot = first_snapshot(saved_bank)
    editor.delete_question(saved_bank.url, snapshot)
    after = saved_bank.snapshot(1)
    assert after["word"] == snapshot["word"]
    assert after["existing"] == snapshot["existing"][1:]
    assert question_status(after) == "stale"
    with pytest.raises(ValueError, match="삭제"):
        editor.load_question(saved_bank.url, snapshot["question_id"])


@pytest.mark.parametrize("action", ["update", "delete"])
def test_database_trigger_mismatch_rolls_back(saved_bank, action):
    snapshot = first_snapshot(saved_bank)
    with psycopg.connect(saved_bank.url) as c:
        body = "NEW.explanation := 'trigger changed'; RETURN NEW;" if action == "update" else "RETURN NULL;"
        c.execute("CREATE FUNCTION topik_bank.test_editor_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ "
                  f"BEGIN {body} END $$")
        c.execute(f"CREATE TRIGGER test_editor BEFORE {action.upper()} ON topik_bank.vocabulary_questions "
                  "FOR EACH ROW EXECUTE FUNCTION topik_bank.test_editor_trigger()")
    with pytest.raises(ValueError, match="취소"):
        if action == "update":
            editor.update_question(saved_bank.url, snapshot, payload(snapshot, explanation="사용자 수정"))
        else:
            editor.delete_question(saved_bank.url, snapshot)
    assert saved_bank.snapshot(1)["existing"] == snapshot["existing"]


def test_page_search_edit_validation_and_confirmed_delete(saved_bank, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", saved_bank.url)
    before = saved_bank.snapshot(1)
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception and saved_bank.snapshot(1) == before
    app.text_input(key="vocab-manager-query").set_value("운동회").run()
    assert len(app.dataframe[0].value) == 1
    assert button(app, "선택 문항 영구 삭제").disabled
    widget(app, "text_area", "-explanation").set_value("화면에서 수정한 해설")
    button(app, "PostgreSQL에 저장").click().run()
    assert not app.exception and app.success
    assert saved_bank.snapshot(1)["existing"][0]["explanation"] == "화면에서 수정한 해설"
    widget(app, "text_input", "-choice-0").set_value("미뤘어요")
    button(app, "PostgreSQL에 저장").click().run()
    assert not app.exception and app.error
    assert saved_bank.snapshot(1)["existing"][0]["choices"] == before["existing"][0]["choices"]
    widget(app, "checkbox", "-confirm").check().run()
    button(app, "선택 문항 영구 삭제").click().run()
    assert not app.exception and app.success and app.info
    assert saved_bank.snapshot(1)["existing"] == before["existing"][1:]
    app.text_input(key="vocab-manager-query").set_value("").run()
    assert not app.exception and len(app.dataframe[0].value) == 6
    assert not widget(app, "checkbox", "-confirm").value


@pytest.mark.parametrize("action", ["update", "delete"])
def test_page_keeps_displayed_snapshot_on_submit(saved_bank, monkeypatch, action):
    monkeypatch.setenv("DATABASE_URL", saved_bank.url)
    app = AppTest.from_file(str(PAGE)).run()
    if action == "update":
        widget(app, "text_area", "-explanation").set_value("이전 화면에서 수정")
    else:
        widget(app, "checkbox", "-confirm").check().run()
    with psycopg.connect(saved_bank.url) as c:
        c.execute("UPDATE topik_bank.vocabulary_questions SET explanation='최신 변경' WHERE position=1")
    button(app, "PostgreSQL에 저장" if action == "update" else "선택 문항 영구 삭제").click().run()
    assert not app.exception and any("변경되었습니다" in error.value for error in app.error)
    assert saved_bank.snapshot(1)["existing"][0]["explanation"] == "최신 변경"
    assert len(saved_bank.snapshot(1)["existing"]) == 7
    button(app, "새로고침").click().run()
    assert widget(app, "text_area", "-explanation").value == "최신 변경"


def test_page_filter_resets_selection_and_confirmation(saved_bank, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", saved_bank.url)
    app = AppTest.from_file(str(PAGE)).run()
    widget(app, "checkbox", "-confirm").check().run()
    app.selectbox(key="vocab-manager-type").set_value("synonym").run()
    assert not app.exception and len(app.dataframe[0].value) == 1
    assert not widget(app, "checkbox", "-confirm").value
    assert not any(item.key and item.key.endswith("-accepted") for item in app.text_area)
    app.selectbox(key="vocab-manager-level").set_value(4).run()
    assert not app.exception and app.info
    assert not any(item.key == "vocab-manager-selected" for item in app.selectbox)


def test_page_edit_removing_last_search_match_can_search_again(saved_bank, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", saved_bank.url)
    app = AppTest.from_file(str(PAGE)).run()
    app.text_input(key="vocab-manager-query").set_value("운동회").run()
    widget(app, "text_area", "-stem").set_value("행사를 다음 주로 ( ).")
    button(app, "PostgreSQL에 저장").click().run()
    assert not app.exception and app.success and app.info
    app.text_input(key="vocab-manager-query").set_value("행사를").run()
    assert not app.exception and len(app.dataframe[0].value) == 1
    assert widget(app, "text_area", "-stem").value == "행사를 다음 주로 ( )."
