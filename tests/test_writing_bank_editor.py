from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import uuid

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from test_writing_postgres import bank, local_set
from test_writing_transfer import data_editor_widget_events
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.question_bank import writing_postgres
from topik_question_lab.common.question_bank.connection import TransferError
from topik_question_lab.common.writing_models import WritingQuestion
from topik_question_lab.menus.writing.writing_pipeline import load_references


APP = str(PACKAGE_ROOT / "menus/writing_bank_editor/writing_bank_editor_app.py")


def check_rows(app, column, values, *, frame_index=0):
    frame = [frame for frame in app.dataframe if column in frame.value.columns][frame_index]
    states = app._tree.get_widget_states()
    edit = next(state for state in states.widgets if state.id == frame.proto.id)
    edit.string_value = json.dumps({"edited_rows": {
        str(index): {"선택": value in values} for index, value in enumerate(frame.value[column])
    }, "added_rows": [], "deleted_rows": []})
    app._run(states)
    assert not app.exception


@pytest.fixture
def ui_bank(monkeypatch):
    questions = [r.question.model_dump() for r in load_references() if r.source_exam == 102]

    class Bank:
        tables_available = True

        def __init__(self):
            self.sets = {}
            self.saves = []
            for i in range(2):
                sid = str(uuid.uuid4())
                self.sets[sid] = {
                    "set_id": sid, "source_key": f"source-{i}", "exam_format": "pbt", "model": "test-model",
                    "created_at": "2026-11-15T00:00:00+00:00", "updated_at": "2026-11-15T00:00:00+00:00",
                    "questions": [{"set_id": sid, "type_slot": q["type_slot"], "source_generated_id": index + 1,
                                   "content_json": deepcopy(q)} for index, q in enumerate(questions)],
                }

        def available(self):
            return self.tables_available

        def list_sets(self):
            return [{**{key: value for key, value in row.items() if key != "questions"},
                     "items": len(row["questions"])} for row in self.sets.values()]

        def get_set(self, source_key):
            return deepcopy(next((row for row in self.sets.values() if row["source_key"] == source_key), None))

        def save_question(self, original, draft):
            row = next(row for row in self.sets[original["set_id"]]["questions"]
                       if row["type_slot"] == original["type_slot"])
            if row != original:
                raise ValueError("문항이 변경되었습니다. 목록을 새로고침하세요.")
            row["content_json"] = WritingQuestion.model_validate(draft).model_dump()
            self.sets[original["set_id"]]["updated_at"] = "2026-11-15T02:00:00+00:00"
            self.saves.append(deepcopy(row))
            return self.sets[original["set_id"]]["updated_at"]

    fake = Bank()
    monkeypatch.setenv("DATABASE_URL", "test-only")
    monkeypatch.setattr(writing_postgres, "WritingQuestionBank", lambda url: fake)
    return fake


def test_ui_checks_sets_then_questions_and_saves_only_selected_item(ui_bank, data_editor_widget_events):
    before = deepcopy(ui_bank.sets)
    ids = list(before)
    app = AppTest.from_file(APP).run()
    assert not app.exception and not app.error
    assert len(app.dataframe) == 1 and not app.text_input
    assert app.dataframe[0].value["Hub 저장(KST)"].tolist() == ["2026/11/15 09:00"] * 2
    check_rows(app, "_set_id", ids[:1])
    assert len(app.dataframe) == 2 and not app.text_input
    assert app.dataframe[1].value["문항 번호"].tolist() == [51, 52, 53, 54]
    check_rows(app, "_type_slot", [51, 52])
    assert len(app.text_area) == 2 and len(app.text_input) == 3
    app.text_area[0].set_value("새 지문 (㉠). 확인 (㉡).")
    app.text_area[1].set_value("아직 저장하지 않은 (㉠). 문항 (㉡).")
    app.run()
    assert ui_bank.sets == before and not ui_bank.saves
    app.button(key=f"writing-bank-save-{ids[0]}-51").click().run()
    assert not app.exception and not app.error and app.success
    assert len(ui_bank.saves) == 1
    assert ui_bank.sets[ids[1]] == before[ids[1]]
    assert ui_bank.sets[ids[0]]["questions"][1:] == before[ids[0]]["questions"][1:]
    assert app.text_area[0].value == "새 지문 (㉠). 확인 (㉡)."
    assert app.text_area[1].value == "아직 저장하지 않은 (㉠). 문항 (㉡)."
    state = app.session_state["writing-bank-state"]
    assert state["selected_sets"] == ids[:1] and state["selected_questions"][ids[0]] == [51, 52]
    assert app.dataframe[0].value["최근 수정(KST)"].iloc[0] == "2026/11/15 11:00"
    check_rows(app, "_type_slot", [])
    assert not app.text_area
    check_rows(app, "_set_id", [])
    assert len(app.dataframe) == 1 and not app.text_input


def test_ui_multiple_sets_and_all_question_editors_have_independent_state(ui_bank, data_editor_widget_events):
    ids = list(ui_bank.sets)
    app = AppTest.from_file(APP).run()
    check_rows(app, "_set_id", ids)
    check_rows(app, "_type_slot", [51, 52, 53, 54])
    check_rows(app, "_type_slot", [51, 52, 53, 54], frame_index=1)
    assert not app.error
    assert len([button for button in app.button if button.label == "Hub PostgreSQL에 저장"]) == 8
    topics = [widget for widget in app.text_input if widget.label == "문항 소재"]
    topics[2].set_value("수정한 자료 소재").run()
    app.button(key=f"writing-bank-save-{ids[0]}-53").click().run()
    assert not app.exception and not app.error
    saved = ui_bank.saves[-1]["content_json"]
    assert saved["topic"] == "수정한 자료 소재" and "수정한 자료 소재" in saved["instruction"]
    assert ui_bank.sets[ids[1]]["questions"][2]["content_json"]["topic"] != saved["topic"]
    requirement = next(widget for widget in app.text_input if widget.label == "요구 사항 1")
    requirement.set_value("수정한 논술 요구 사항").run()
    app.button(key=f"writing-bank-save-{ids[0]}-54").click().run()
    assert not app.exception and not app.error
    assert ui_bank.saves[-1]["content_json"]["requirements"][0] == "수정한 논술 요구 사항"


def test_ui_validation_and_stale_snapshot_prevent_saving(ui_bank, data_editor_widget_events):
    sid = next(iter(ui_bank.sets))
    app = AppTest.from_file(APP).run()
    check_rows(app, "_set_id", [sid])
    check_rows(app, "_type_slot", [51])
    app.text_area[0].set_value("빈칸이 없는 지문").run()
    assert not app.exception and app.warning
    assert app.button(key=f"writing-bank-save-{sid}-51").disabled
    app.text_area[0].set_value("편집 (㉠). 내용 (㉡).").run()
    ui_bank.sets[sid]["questions"][0]["content_json"]["passage"] = "다른 사용자 (㉠). 저장 (㉡)."
    app.button(key=f"writing-bank-save-{sid}-51").click().run()
    assert not app.exception and app.error and not ui_bank.saves
    assert app.text_area[0].value == "편집 (㉠). 내용 (㉡)."
    app.button(key="writing-bank-refresh").click().run()
    assert not app.exception and not app.error
    assert app.text_area[0].value == "다른 사용자 (㉠). 저장 (㉡)."


def test_ui_refresh_keeps_selection_by_id_after_reordering_and_deletion(ui_bank, data_editor_widget_events):
    ids = list(ui_bank.sets)
    app = AppTest.from_file(APP).run()
    check_rows(app, "_set_id", ids[:1])
    check_rows(app, "_type_slot", [54])
    ui_bank.sets = dict(reversed(list(ui_bank.sets.items())))
    app.button(key="writing-bank-refresh").click().run()
    assert not app.exception and not app.error
    assert app.session_state["writing-bank-state"]["selected_sets"] == ids[:1]
    assert len([widget for widget in app.text_input if widget.label == "요구 사항 1"]) == 1
    del ui_bank.sets[ids[0]]
    app.button(key="writing-bank-refresh").click().run()
    assert not app.exception and not app.text_input
    assert app.session_state["writing-bank-state"]["selected_sets"] == []


@pytest.mark.parametrize("case", ["no_connection", "no_tables", "empty", "connection_error", "ibt"])
def test_ui_unavailable_states_are_read_only(ui_bank, monkeypatch, data_editor_widget_events, case):
    if case == "no_connection":
        monkeypatch.setenv("DATABASE_URL", "")
    elif case == "no_tables":
        ui_bank.tables_available = False
    elif case == "empty":
        ui_bank.sets.clear()
    elif case == "connection_error":
        def fail():
            raise TransferError("DB 연결 오류")
        monkeypatch.setattr(ui_bank, "available", fail)
    else:
        next(iter(ui_bank.sets.values()))["exam_format"] = "ibt"
    app = AppTest.from_file(APP).run()
    if case == "ibt":
        check_rows(app, "_set_id", [next(iter(ui_bank.sets))])
    assert not app.exception and not app.text_input and not ui_bank.saves
    assert app.error if case == "connection_error" else app.info


def test_database_edit_preserves_identity_and_other_questions_and_rejects_conflict(local_set, bank):
    storage, run_id = local_set
    rows = storage.list_generated(run_id)
    bank.save_set("first", rows)
    bank.save_set("second", rows)
    before = bank.get_set("first")
    other = bank.get_set("second")
    assert len(bank.list_sets()) == 2 and all(row["items"] == 4 for row in bank.list_sets())
    original = before["questions"][0]
    draft = {**original["content_json"], "passage": "Hub 수정 (㉠). 저장 (㉡)."}
    bank.save_question(original, draft)
    after = bank.get_set("first")
    assert after["set_id"] == before["set_id"] and after["created_at"] == before["created_at"]
    assert after["updated_at"] > before["updated_at"]
    assert after["questions"][0] == {**original, "content_json": draft}
    assert after["questions"][1:] == before["questions"][1:]
    assert bank.get_set("second") == other
    with pytest.raises(ValueError, match="변경되었거나 삭제"):
        bank.save_question(original, {**draft, "passage": "지난 화면 (㉠). 저장 (㉡)."})
    assert bank.get_set("first") == after
    # Another question in the same set remains editable from its displayed snapshot.
    original52 = before["questions"][1]
    bank.save_question(original52, {**original52["content_json"], "passage": "52번 수정 (㉠). 저장 (㉡)."})
    assert storage.list_generated(run_id) == rows


@pytest.mark.parametrize("case", ["invalid", "slot", "ibt", "deleted"])
def test_database_rejects_invalid_or_missing_question_without_changes(local_set, bank, case):
    storage, run_id = local_set
    bank.save_set("test", storage.list_generated(run_id))
    snapshot = bank.get_set("test")
    original = snapshot["questions"][0]
    draft = deepcopy(original["content_json"])
    if case == "invalid":
        draft["passage"] = "빈칸 없음"
    elif case == "slot":
        draft = deepcopy(snapshot["questions"][1]["content_json"])
    else:
        with psycopg.connect(bank.url) as c:
            if case == "ibt":
                c.execute("UPDATE topik_bank.writing_sets SET exam_format='ibt'")
            else:
                c.execute("DELETE FROM topik_bank.writing_questions WHERE type_slot=51")
    before = bank.get_set("test")
    with pytest.raises(ValueError):
        bank.save_question(original, draft)
    assert bank.get_set("test") == before


def test_database_timestamp_failure_rolls_back_question_edit(local_set, bank):
    storage, run_id = local_set
    bank.save_set("test", storage.list_generated(run_id))
    before = bank.get_set("test")
    original = before["questions"][0]
    with psycopg.connect(bank.url) as c:
        c.execute("""CREATE FUNCTION topik_bank.fail_writing_update() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected failure'; END $$""")
        c.execute("""CREATE TRIGGER fail_writing_update BEFORE UPDATE ON topik_bank.writing_sets
            FOR EACH ROW EXECUTE FUNCTION topik_bank.fail_writing_update()""")
    with pytest.raises(TransferError):
        bank.save_question(original, {**original["content_json"], "passage": "실패 (㉠). 복원 (㉡)."})
    assert bank.get_set("test") == before


def test_concurrent_edits_only_accept_one_displayed_snapshot(local_set, bank):
    storage, run_id = local_set
    bank.save_set("test", storage.list_generated(run_id))
    original = bank.get_set("test")["questions"][0]

    def save(index):
        try:
            bank.save_question(original, {**original["content_json"], "passage": f"동시 수정 {index} (㉠). 저장 (㉡)."})
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [1, 2]))
    assert sorted(results) == [False, True]


def test_ui_roundtrip_to_postgres(local_set, bank, monkeypatch, data_editor_widget_events):
    storage, run_id = local_set
    bank.save_set("test", storage.list_generated(run_id))
    before = bank.get_set("test")
    sid = before["set_id"]
    monkeypatch.setenv("DATABASE_URL", bank.url)
    app = AppTest.from_file(APP).run()
    check_rows(app, "_set_id", [sid])
    check_rows(app, "_type_slot", [53])
    next(widget for widget in app.text_input if widget.label == "자료 출처").set_value("수정한 조사 출처").run()
    assert bank.get_set("test") == before
    app.button(key=f"writing-bank-save-{sid}-53").click().run()
    assert not app.exception and not app.error and app.success
    after = bank.get_set("test")
    assert after["questions"][2]["content_json"]["data_source"] == "수정한 조사 출처"
    assert after["questions"][:2] == before["questions"][:2]
    assert after["questions"][3] == before["questions"][3]
    app.button(key="writing-bank-refresh").click().run()
    assert not app.exception
    assert next(widget for widget in app.text_input if widget.label == "자료 출처").value == "수정한 조사 출처"
