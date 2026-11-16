from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import uuid

import psycopg
from psycopg.types.json import Jsonb
import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.question_bank.connection import TransferError, connection, digest
from topik_question_lab.common.question_bank.writing_postgres import WritingQuestionBank
from topik_question_lab.menus.writing.writing_pipeline import load_references
from topik_question_lab.menus.writing_transfer import writing_transfer as transfer


APP = str(PACKAGE_ROOT / "menus/writing_transfer/writing_transfer_app.py")


def create_tables(url):
    with psycopg.connect(url) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute(transfer.SCHEMA_FILE.read_text(encoding="utf-8"))


@pytest.fixture
def banks(postgres_database_factory, writing_png, writing_cloud):
    source, target = postgres_database_factory(), postgres_database_factory()
    create_tables(source)
    questions = [r.question.model_dump() for r in load_references() if r.source_exam == 102]
    writing_png(next(q for q in questions if q["type_slot"] == 53))
    bank = WritingQuestionBank(source)
    ids = []
    for offset in (0, 100):
        records = [{"id": offset + q["type_slot"], "run_id": offset + 1, "provider": "test",
                    "model": "test-model", "question": q} for q in questions]
        ids.append(bank.save_set(f"writing:pbt:{offset}", records)["set_id"])
    return source, target, ids


def read_rows(url, ids):
    with connection(url) as c:
        return transfer._rows(c, ids) if transfer._schema(c) else None


def send(source, target, ids, root):
    return transfer.execute_writing_transfer(source, target,
        transfer.prepare_writing_transfer(transfer.capture_writing(source, ids), target), root)


def edit_passage(url, sid, text):
    with psycopg.connect(url) as c:
        c.execute("""UPDATE topik_bank.writing_questions SET content_json=jsonb_set(content_json,'{passage}',%s)
            WHERE set_id=%s AND type_slot=51""", (Jsonb(text), sid))


def test_preview_is_read_only_initial_registration_and_retry_are_exact(banks, tmp_path):
    source, target, ids = banks
    original = read_rows(source, ids)
    assert all(q["content_json"]["image"]["storage_backend"] == "s3"
               for q in original["writing_questions"] if q["type_slot"] == 53)
    catalog = transfer.list_source_sets(source)
    assert {r["set_id"] for r in catalog} == set(ids)
    assert all(r["items"] == 4 and r["exam_format"] == "pbt" for r in catalog)
    snapshot = transfer.capture_writing(source, ids)
    plan = transfer.prepare_writing_transfer(snapshot, target)
    assert plan.create_tables and plan.effects == {"created": 2, "updated": 0, "unchanged": 0}
    assert read_rows(target, ids) is None
    assert not (tmp_path / "backups").exists()
    result = transfer.execute_writing_transfer(source, target, plan, tmp_path)
    assert result["status"] == "verified" and result["sets"] == 2 and result["questions"] == 8
    assert read_rows(source, ids) == read_rows(target, ids) == original
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    assert backup["schema"] is None and backup["rows"] == {t: [] for t in transfer.FIELDS}
    assert source not in json.dumps(backup) and target not in json.dumps(backup)
    with connection(target) as c:
        assert {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='topik_bank'")} == set(transfer.FIELDS)
    retry = send(source, target, ids, tmp_path)
    assert retry["unchanged"] == 2 and retry["created"] == retry["updated"] == 0 and retry["backup"] is None
    assert read_rows(target, ids) == original


def test_updates_preserve_other_sets_and_backup_previous_selected_contents(banks, tmp_path):
    source, target, ids = banks
    send(source, target, ids, tmp_path)
    before_selected = read_rows(target, ids[:1])
    before_other = read_rows(target, ids[1:])
    with psycopg.connect(target) as c:
        c.execute("CREATE TABLE public.unrelated_data(value text)")
        c.execute("INSERT INTO public.unrelated_data VALUES ('keep')")
    edit_passage(source, ids[0], "변경한 내용 (㉠). 이어지는 내용 (㉡).")
    result = send(source, target, ids[:1], tmp_path)
    assert result["updated"] == 1 and result["created"] == 0
    assert read_rows(target, ids[:1]) == read_rows(source, ids[:1])
    assert read_rows(target, ids[1:]) == before_other
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    assert backup["rows"] == before_selected
    assert backup["content_hash"] == digest((backup["schema"], backup["rows"]))
    with psycopg.connect(target) as c:
        assert c.execute("SELECT value FROM public.unrelated_data").fetchone()[0] == "keep"


@pytest.mark.parametrize("case", ["missing_question", "invalid_question", "stale_image", "ibt", "missing_set", "empty_selection"])
def test_invalid_sources_are_blocked(banks, case):
    source, target, ids = banks
    with psycopg.connect(source) as c:
        if case == "missing_question":
            c.execute("DELETE FROM topik_bank.writing_questions WHERE set_id=%s AND type_slot=52", (ids[0],))
        elif case == "invalid_question":
            c.execute("UPDATE topik_bank.writing_questions SET content_json=jsonb_set(content_json,'{materials}','[]') WHERE set_id=%s AND type_slot=53", (ids[0],))
        elif case == "stale_image":
            c.execute("UPDATE topik_bank.writing_questions SET content_json=jsonb_set(content_json,'{data_source}',%s) WHERE set_id=%s AND type_slot=53",
                      (Jsonb("이미지 생성 후 변경된 자료"), ids[0]))
        elif case == "ibt":
            c.execute("UPDATE topik_bank.writing_sets SET exam_format='ibt' WHERE set_id=%s", (ids[0],))
    selected = [] if case == "empty_selection" else [str(uuid.uuid4())] if case == "missing_set" else ids[:1]
    with pytest.raises(TransferError):
        transfer.capture_writing(source, selected)
    assert read_rows(target, ids) is None


def test_source_cannot_be_a_destination(banks):
    source, _, ids = banks
    snapshot = transfer.capture_writing(source, ids)
    with pytest.raises(TransferError, match="원본"):
        transfer.prepare_writing_transfer(snapshot, source)


@pytest.mark.parametrize("case", ["same_id", "same_source_key"])
def test_identity_collisions_do_not_replace_target_data(banks, tmp_path, case):
    source, target, ids = banks
    create_tables(target)
    snapshot = transfer.capture_writing(source, ids[:1])
    conflicting = deepcopy(snapshot.rows)
    if case == "same_id":
        conflicting["writing_sets"][0]["source_key"] = "another-origin"
    else:
        other_id = str(uuid.uuid4())
        for rows in conflicting.values():
            for row in rows:
                row["set_id"] = other_id
    with connection(target, readonly=False) as c:
        for table in transfer.FIELDS:
            transfer._upsert(c, table, conflicting[table])
    with pytest.raises(TransferError, match="충돌"):
        transfer.prepare_writing_transfer(snapshot, target)
    other_ids = [r["set_id"] for r in conflicting["writing_sets"]]
    assert read_rows(target, other_ids) == conflicting
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("case", ["partial", "column", "constraint", "rls", "trigger"])
def test_incompatible_target_schema_is_never_modified(banks, case):
    source, target, ids = banks
    create_tables(target)
    with psycopg.connect(target) as c:
        if case == "partial":
            c.execute("DROP TABLE topik_bank.writing_questions")
        elif case == "column":
            c.execute("ALTER TABLE topik_bank.writing_sets ADD COLUMN extra text")
        elif case == "constraint":
            c.execute("ALTER TABLE topik_bank.writing_sets ADD CONSTRAINT pbt_only CHECK (exam_format='pbt')")
        elif case == "rls":
            c.execute("ALTER TABLE topik_bank.writing_sets ENABLE ROW LEVEL SECURITY")
        else:
            c.execute("CREATE FUNCTION public.extra_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
            c.execute("CREATE TRIGGER extra_trigger BEFORE INSERT ON topik_bank.writing_sets FOR EACH ROW EXECUTE FUNCTION public.extra_trigger()")
    with pytest.raises(TransferError):
        transfer.prepare_writing_transfer(transfer.capture_writing(source, ids), target)
    with psycopg.connect(target) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.writing_sets").fetchone()[0] == 0


@pytest.mark.parametrize("side", ["source", "target"])
def test_data_changed_after_preview_requires_new_preview(banks, tmp_path, side):
    source, target, ids = banks
    send(source, target, ids, tmp_path)
    plan = transfer.prepare_writing_transfer(transfer.capture_writing(source, ids[:1]), target)
    edit_passage(source if side == "source" else target, ids[0], "새로운 수정 (㉠). 확인 (㉡).")
    before = read_rows(target, ids)
    with pytest.raises(TransferError, match="미리보기 이후"):
        transfer.execute_writing_transfer(source, target, plan, tmp_path)
    assert read_rows(target, ids) == before


def test_target_change_outside_selection_is_preserved(banks, tmp_path):
    source, target, ids = banks
    send(source, target, ids, tmp_path)
    edit_passage(source, ids[0], "선택한 원본 (㉠). 수정 (㉡).")
    plan = transfer.prepare_writing_transfer(transfer.capture_writing(source, ids[:1]), target)
    edit_passage(target, ids[1], "다른 세트 (㉠). 보존 (㉡).")
    other = read_rows(target, ids[1:])
    assert transfer.execute_writing_transfer(source, target, plan, tmp_path)["updated"] == 1
    assert read_rows(target, ids[1:]) == other


@pytest.mark.parametrize("existing", [False, True])
def test_partial_write_rolls_back_rows_and_table_creation(banks, tmp_path, monkeypatch, existing):
    source, target, ids = banks
    if existing:
        send(source, target, ids, tmp_path)
    before = read_rows(target, ids)
    edit_passage(source, ids[0], "수정할 원본 (㉠). 롤백 (㉡).")
    plan = transfer.prepare_writing_transfer(transfer.capture_writing(source, ids), target)
    original = transfer._upsert

    def fail(c, table, rows):
        if table == "writing_questions":
            raise RuntimeError("private driver details")
        original(c, table, rows)

    monkeypatch.setattr(transfer, "_upsert", fail)
    with pytest.raises(TransferError, match="롤백") as error:
        transfer.execute_writing_transfer(source, target, plan, tmp_path)
    assert "private driver details" not in str(error.value)
    assert read_rows(target, ids) == before
    assert list((tmp_path / "backups/writing").glob("*.json"))


def test_backup_failure_blocks_any_destination_change(banks, tmp_path, monkeypatch):
    source, target, ids = banks
    plan = transfer.prepare_writing_transfer(transfer.capture_writing(source, ids), target)
    original = Path.open

    def fail(path, *args, **kwargs):
        if path.suffix == ".json" and args and args[0] == "x":
            raise OSError("private filesystem details")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(TransferError, match="백업에 실패"):
        transfer.execute_writing_transfer(source, target, plan, tmp_path)
    assert read_rows(target, ids) is None


def test_lost_commit_response_can_be_verified_and_retried_without_duplicates(banks, tmp_path, monkeypatch):
    source, target, ids = banks
    plan = transfer.prepare_writing_transfer(transfer.capture_writing(source, ids), target)
    original = transfer.connection

    @contextmanager
    def lost_response(url, *, readonly=True):
        with original(url, readonly=readonly) as c:
            yield c
        if url == target and not readonly:
            raise TransferError("lost commit response")

    monkeypatch.setattr(transfer, "connection", lost_response)
    with pytest.raises(TransferError, match="커밋 결과를 확정하지 못했습니다"):
        transfer.execute_writing_transfer(source, target, plan, tmp_path)
    monkeypatch.setattr(transfer, "connection", original)
    assert read_rows(target, ids) == read_rows(source, ids)
    assert send(source, target, ids, tmp_path)["unchanged"] == 2


def test_post_commit_read_failure_does_not_report_a_rollback(banks, tmp_path, monkeypatch):
    source, target, ids = banks
    original = transfer._verify
    calls = []

    def verify(c, snapshot):
        calls.append(1)
        if len(calls) == 2:
            raise TransferError("read failure")
        original(c, snapshot)

    monkeypatch.setattr(transfer, "_verify", verify)
    with pytest.raises(TransferError, match="커밋되었지만 최종 조회 검증"):
        send(source, target, ids, tmp_path)
    assert read_rows(target, ids) == read_rows(source, ids)


def configure_ui(monkeypatch, source, local, production):
    monkeypatch.setenv("DATABASE_URL", source)
    monkeypatch.setenv("QUESTION_BANK_LOCAL_DATABASE_URL", local)
    monkeypatch.setenv("QUESTION_BANK_PRODUCTION_DATABASE_URL", production)


@pytest.fixture
def data_editor_widget_events(monkeypatch):
    from streamlit.proto.WidgetStates_pb2 import WidgetState
    import streamlit.testing.v1.element_tree as elements

    original = elements.get_widget_state

    def widget_state(node):
        # AppTest omits data editors; preserve the edits a browser resends on clicks.
        if isinstance(node, elements.Dataframe) and node.proto.id and node.proto.editing_mode:
            return WidgetState(id=node.proto.id, string_value=json.dumps(node.root.session_state[node.proto.id]))
        return original(node)

    monkeypatch.setattr(elements, "get_widget_state", widget_state)


def select_table_sets(app, set_ids):
    table = next(frame for frame in app.dataframe if "_set_id" in frame.value.columns)
    states = app._tree.get_widget_states()
    edit = next((state for state in states.widgets if state.id == table.proto.id), None)
    if edit is None:
        edit = states.widgets.add(id=table.proto.id)
    edit.string_value = json.dumps({"edited_rows": {
        str(index): {"선택": sid in set_ids} for index, sid in enumerate(table.value["_set_id"])
    }, "added_rows": [], "deleted_rows": []})
    app._run(states)
    assert not app.exception
    assert set(app.session_state["writing-transfer-selected"]) == set(set_ids)


def test_ui_server_results_are_independent_and_failed_server_can_retry(banks, postgres_database_factory, tmp_path, monkeypatch, data_editor_widget_events):
    source, local, ids = banks
    production = postgres_database_factory()
    configure_ui(monkeypatch, source, local, production)
    original = transfer.execute_writing_transfer

    def fail_production(source_url, target_url, plan, root):
        if target_url == production:
            raise TransferError("운영 서버 테스트 실패")
        return original(source_url, target_url, plan, root)

    monkeypatch.setattr(transfer, "execute_writing_transfer", fail_production)
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.multiselect(key="writing-transfer-targets").set_value(["로컬 서버", "운영 서버"]).run()
    select_table_sets(app, ids[:1])
    app.button(key="writing-transfer-preview-button").click().run()
    assert not app.exception and not app.error
    assert read_rows(local, ids) is None and read_rows(production, ids) is None
    app.button(key="writing-transfer-execute").click().run()
    assert not app.exception
    assert any("로컬 서버: 세트 1개" in item.value for item in app.success)
    assert any("운영 서버 테스트 실패" in item.value for item in app.warning)
    assert read_rows(local, ids[:1]) == read_rows(source, ids[:1])
    assert read_rows(local, ids[1:]) == {table: [] for table in transfer.FIELDS}
    assert read_rows(production, ids) is None
    monkeypatch.setattr(transfer, "execute_writing_transfer", original)
    app.multiselect(key="writing-transfer-targets").set_value(["운영 서버"]).run()
    app.button(key="writing-transfer-preview-button").click().run()
    app.button(key="writing-transfer-execute").click().run()
    assert not app.exception and any("운영 서버: 세트 1개" in item.value for item in app.success)
    assert read_rows(production, ids[:1]) == read_rows(source, ids[:1])


def test_ui_rejects_duplicate_destinations_and_invalidates_old_previews(banks, monkeypatch, data_editor_widget_events):
    source, target, ids = banks
    configure_ui(monkeypatch, source, target, target)
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.multiselect(key="writing-transfer-targets").set_value(["로컬 서버", "운영 서버"]).run()
    select_table_sets(app, ids[:1])
    app.button(key="writing-transfer-preview-button").click().run()
    assert not app.exception and any("같은 대상 DB" in item.value for item in app.error)
    assert app.button(key="writing-transfer-execute").disabled
    app.multiselect(key="writing-transfer-targets").set_value(["로컬 서버"]).run()
    assert not any(b.key == "writing-transfer-execute" for b in app.button)
    app.button(key="writing-transfer-preview-button").click().run()
    assert not app.button(key="writing-transfer-execute").disabled
    select_table_sets(app, [])
    assert app.button(key="writing-transfer-preview-button").disabled
    assert not any(b.key == "writing-transfer-execute" for b in app.button)
    select_table_sets(app, ids[1:])
    assert not any(b.key == "writing-transfer-execute" for b in app.button)
    app.button(key="writing-transfer-preview-button").click().run()
    app.button(key="writing-transfer-refresh").click().run()
    assert not any(b.key == "writing-transfer-execute" for b in app.button)
    assert read_rows(target, ids) is None


def test_selection_table_refresh_preserves_ids_when_rows_move_or_disappear(monkeypatch, data_editor_widget_events):
    ids = [str(uuid.uuid4()) for _ in range(3)]
    catalog = [{"set_id": sid, "exam_format": "pbt", "model": "test-model", "items": 4,
                "created_at": "2026-11-15T00:00:00+00:00", "updated_at": "2026-11-15T01:00:00+00:00"}
               for sid in ids]
    configure_ui(monkeypatch, "test-source", "test-target", "")
    monkeypatch.setattr(transfer, "list_source_sets", lambda url: deepcopy(catalog))
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert not app.exception and not app.error
    assert [widget.label for widget in app.multiselect] == ["등록 대상"]
    table = app.dataframe[0].value
    assert table["_set_id"].tolist() == ids and not table["선택"].any()
    assert table["Hub 저장(KST)"].tolist() == ["2026/11/15 09:00"] * 3
    assert app.button(key="writing-transfer-preview-button").disabled
    select_table_sets(app, ids[:2])
    assert any(c.value == "선택 2개 세트 · 총 8문항" for c in app.caption)
    catalog.reverse()
    app.button(key="writing-transfer-refresh").click().run()
    assert not app.exception
    table = app.dataframe[0].value
    assert table["_set_id"].tolist() == list(reversed(ids))
    assert set(table.loc[table["선택"], "_set_id"]) == set(ids[:2])
    catalog[:] = [row for row in catalog if row["set_id"] != ids[0]]
    app.button(key="writing-transfer-refresh").click().run()
    assert not app.exception and app.session_state["writing-transfer-selected"] == ids[1:2]
    catalog.clear()
    app.button(key="writing-transfer-refresh").click().run()
    assert not app.exception and not app.session_state["writing-transfer-selected"]
    assert app.button(key="writing-transfer-preview-button").disabled
