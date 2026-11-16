from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import uuid

import psycopg
from psycopg.types.json import Jsonb
import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.generation.providers import manual_result
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.question_bank import writing_postgres
from topik_question_lab.common.question_bank.connection import TransferError
from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.maintenance import bank_migrations
from topik_question_lab.menus.writing.writing_pipeline import load_references, save_result


APP = str(PACKAGE_ROOT / "menus/writing/writing_app.py")


@pytest.fixture
def local_set(tmp_path, writing_png):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    questions = [r.question.model_dump() for r in load_references() if r.source_exam == 102]
    questions.sort(key=lambda q: q["type_slot"])
    result = manual_result("test", "test-model", "generation", json.dumps({"questions": questions}))
    receipt = save_result(storage, result, "system", "user", [51, 52, 53, 54])
    assert not receipt["error"]
    writing_png(next(q for q in questions if q["type_slot"] == 53))
    return storage, receipt["run_id"]


@pytest.fixture
def bank(postgres_database_factory, writing_cloud):
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    return writing_postgres.WritingQuestionBank(url)


def test_source_identity_is_read_only_until_save_and_survives_reload(local_set, tmp_path):
    storage, run_id = local_set
    assert storage.publication_source_key(run_id) is None
    with storage.connect() as c:
        assert not c.execute("SELECT 1 FROM settings WHERE key LIKE 'writing-set-source:%'").fetchall()
    source = storage.publication_source_key(run_id, create=True)
    reloaded = WritingStorage(storage.path)
    assert source == reloaded.publication_source_key(run_id, create=True)
    assert reloaded.list_generated(run_id) == storage.list_generated()
    assert reloaded.list_generated(run_id + 1) == []
    with pytest.raises(ValueError, match="생성 실행"):
        reloaded.publication_source_key(run_id + 1, create=True)
    other = WritingStorage(tmp_path / "other.db")
    with other.connect() as c:
        c.execute("INSERT INTO runs(id,provider,model,operation,prompt_system,prompt_user,result_json) VALUES (1,'test','test','generation','','','{}')")
        c.execute("INSERT INTO generated_questions(run_id,provider,model,position,data_json) VALUES (1,'test','test',1,'{}')")
    assert other.publication_source_key(1, create=True) != source


@pytest.fixture
def individual_set(tmp_path, writing_png):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    for ref in (r for r in load_references() if r.source_exam == 102):
        slot = ref.question.type_slot
        result = manual_result(f"service-{slot}", f"model-{slot}", "generation",
                               json.dumps({"questions": [ref.question.model_dump()]}))
        assert not save_result(storage, result, "system", "user", [slot])["error"]
        if slot == 53:
            writing_png(ref.question)
    return storage


def test_selected_identity_survives_reordering_and_preserves_old_sets(local_set, individual_set):
    legacy, run_id = local_set
    legacy_ids = [row["id"] for row in legacy.list_generated(run_id)]
    source = legacy.publication_source_key(run_id, create=True)
    assert legacy.selection_source_key(legacy_ids, create=True) == source
    storage = individual_set
    ids = [row["id"] for row in storage.list_generated() if row["run_id"] != run_id]
    assert storage.selection_source_key(ids) is None
    source = storage.selection_source_key(ids, create=True)
    assert WritingStorage(storage.path).selection_source_key(list(reversed(ids)), create=True) == source
    with pytest.raises(ValueError):
        storage.selection_source_key(ids[:3], create=True)
    with pytest.raises(ValueError):
        storage.selection_source_key([ids[0]] * 4, create=True)


def test_individual_questions_with_different_models_form_complete_set(individual_set):
    rows = individual_set.list_generated()
    assert len({row["run_id"] for row in rows}) == 4
    questions = writing_postgres.validate_writing_set(rows)
    assert [q.type_slot for _, q in questions] == [51, 52, 53, 54]


def test_individual_selection_publishes_latest_edits_and_keeps_identity(individual_set, monkeypatch):
    from topik_question_lab.menus.writing import writing_publication_ui

    published = {}
    calls = []

    class Bank:
        def __init__(self, url):
            pass

        def available(self):
            return True

        def get_set(self, source):
            return published.get(source)

        def save_set(self, source, rows, *, image_directory=None):
            writing_postgres.validate_writing_set(rows)
            created = source not in published
            published[source] = {"set_id": source, "rows": deepcopy(rows)}
            calls.append((source, deepcopy(rows)))
            return {"set_id": source, "created": created}

    monkeypatch.setattr(writing_publication_ui, "WritingQuestionBank", Bank)
    monkeypatch.setenv("DATABASE_URL", "test-only")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("6. Hub 세트 저장").run()
    assert not app.exception
    row = next(r for r in individual_set.list_generated() if r["question"]["type_slot"] == 51)
    edited = {**row["question"], "passage": "새 요청 (㉠). 확인 (㉡)."}
    individual_set.save_generated_edit(row["id"], edited)
    app.button(key="writing-publish").click().run()
    assert not app.exception and not app.error
    assert next(r for r in calls[0][1] if r["id"] == row["id"])["question"]["passage"] == edited["passage"]
    app.button(key="writing-publish").click().run()
    assert not app.exception and not app.error
    assert len(published) == 1 and calls[0][0] == calls[1][0]
    result = manual_result("other", "other-model", "generation", json.dumps({"questions": [edited]}))
    new_id = save_result(individual_set, result, "s", "u", [51])["question_ids"][0]
    app.run()
    app.selectbox(key="writing-publish-51").set_value(new_id).run()
    app.button(key="writing-publish").click().run()
    assert not app.exception and not app.error
    assert len(published) == 2 and calls[-1][0] != calls[0][0]


def test_individual_set_database_roundtrip_and_retry(individual_set, bank):
    rows = individual_set.list_generated()
    source = individual_set.selection_source_key([row["id"] for row in rows], create=True)
    receipt = bank.save_set(source, rows)
    assert receipt["created"]
    saved = bank.get_set(source)
    assert saved["provider"] == "service-51 / service-52 / service-53 / service-54"
    assert saved["model"] == "model-51 / model-52 / model-53 / model-54"
    assert len(saved["questions"]) == 4
    retry = bank.save_set(source, list(reversed(rows)))
    assert retry == {**receipt, "created": False}


@pytest.mark.parametrize("case", ["missing", "duplicate", "duplicate_id", "invalid_content", "ibt"])
def test_invalid_sets_are_rejected_before_connecting(local_set, monkeypatch, case):
    storage, run_id = local_set
    rows = storage.list_generated(run_id)
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows[0]["question"] = deepcopy(rows[1]["question"])
    elif case == "duplicate_id":
        rows[0]["id"] = rows[1]["id"]
    elif case == "invalid_content":
        rows[0]["question"]["requirements"] = []
    else:
        rows[0]["question"]["exam_format"] = "ibt"
    monkeypatch.setattr(writing_postgres, "connection", lambda *a, **k: pytest.fail("invalid sets must not connect"))
    with pytest.raises(ValueError):
        writing_postgres.WritingQuestionBank("unused").save_set("source", rows)


def test_save_retry_and_edit_preserve_full_questions(local_set, bank):
    storage, run_id = local_set
    source = storage.publication_source_key(run_id, create=True)
    rows = storage.list_generated(run_id)
    assert bank.available() and bank.get_set(source) is None
    first = bank.save_set(source, rows)
    assert first["created"]
    retry = bank.save_set(source, rows)
    assert retry == {"set_id": first["set_id"], "created": False}
    snapshot = bank.get_set(source)
    assert snapshot["exam_format"] == "pbt"
    assert [{k: v for k, v in q["content_json"].items() if k != "image"} for q in snapshot["questions"]] == [q.model_dump() for _, q in writing_postgres.validate_writing_set(rows)]
    assert snapshot["questions"][2]["content_json"]["image"]["storage_backend"] == "s3"
    row = next(r for r in rows if r["question"]["type_slot"] == 51)
    edit = {**row["question"], "passage": "새로 신청하려고 (㉠). 언제까지 (㉡)?"}
    storage.save_generated_edit(row["id"], edit)
    bank.save_set(source, storage.list_generated(run_id))
    updated = bank.get_set(source)
    assert updated["created_at"] == snapshot["created_at"]
    assert updated["updated_at"] >= snapshot["updated_at"]
    assert updated["questions"][0]["content_json"]["passage"] == edit["passage"]
    with psycopg.connect(bank.url) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.writing_sets").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM topik_bank.writing_questions").fetchone()[0] == 4
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 0


def test_concurrent_retries_create_only_one_set(local_set, bank):
    storage, run_id = local_set
    source = storage.publication_source_key(run_id, create=True)
    rows = storage.list_generated(run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: bank.save_set(source, rows), range(2)))
    assert len({r["set_id"] for r in results}) == 1
    assert sorted(r["created"] for r in results) == [False, True]
    assert len(bank.get_set(source)["questions"]) == 4


@pytest.mark.parametrize("existing", [False, True])
def test_failure_on_third_question_rolls_back_entire_set(local_set, bank, existing):
    storage, run_id = local_set
    source = storage.publication_source_key(run_id, create=True)
    rows = storage.list_generated(run_id)
    if existing:
        bank.save_set(source, rows)
    before = bank.get_set(source)
    with psycopg.connect(bank.url) as c:
        c.execute("""CREATE FUNCTION topik_bank.fail_writing_save() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.type_slot=53 THEN RAISE EXCEPTION 'injected failure'; END IF; RETURN NEW; END $$""")
        c.execute("""CREATE TRIGGER fail_writing_save BEFORE INSERT OR UPDATE ON topik_bank.writing_questions
            FOR EACH ROW EXECUTE FUNCTION topik_bank.fail_writing_save()""")
    next(r for r in rows if r["question"]["type_slot"] == 51)["question"]["passage"] = "변경 (㉠). 실패 (㉡)."
    with pytest.raises(TransferError, match="SQLSTATE"):
        bank.save_set(source, rows)
    assert bank.get_set(source) == before
    with psycopg.connect(bank.url) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.writing_questions").fetchone()[0] == (4 if existing else 0)


def test_schema_accepts_both_exam_formats_and_enforces_constraints(bank):
    with psycopg.connect(bank.url) as c:
        ids = [uuid.uuid4(), uuid.uuid4()]
        for set_id, exam in zip(ids, ("pbt", "ibt")):
            c.execute("""INSERT INTO topik_bank.writing_sets(set_id,source_key,exam_format,provider,model)
                VALUES (%s,%s,%s,'test','test')""", (set_id, exam, exam))
        with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
            c.execute("UPDATE topik_bank.writing_sets SET exam_format='unknown' WHERE set_id=%s", (ids[0],))
        with pytest.raises(psycopg.errors.UniqueViolation), c.transaction():
            c.execute("INSERT INTO topik_bank.writing_sets(set_id,source_key,provider,model) VALUES (%s,'pbt','test','test')", (uuid.uuid4(),))
        c.execute("""INSERT INTO topik_bank.writing_questions VALUES (%s,51,1,%s)""", (ids[0], Jsonb({"type_slot": 51})))
        with pytest.raises(psycopg.errors.UniqueViolation), c.transaction():
            c.execute("INSERT INTO topik_bank.writing_questions VALUES (%s,51,2,%s)", (ids[0], Jsonb({"type_slot": 51})))
        with pytest.raises(psycopg.IntegrityError, match="writing_questions_set_id_fkey"), c.transaction():
            c.execute("DELETE FROM topik_bank.writing_sets WHERE set_id=%s", (ids[0],))
        with pytest.raises(psycopg.errors.ForeignKeyViolation), c.transaction():
            c.execute("INSERT INTO topik_bank.writing_questions VALUES (%s,51,1,%s)", (uuid.uuid4(), Jsonb({"type_slot": 51})))
        for slot, source_id, content in [(50, 1, {"type_slot": 50}), (52, 0, {"type_slot": 52}), (52, 2, []), (52, 2, {}), (52, 2, {"type_slot": 53})]:
            with pytest.raises(psycopg.errors.CheckViolation), c.transaction():
                c.execute("INSERT INTO topik_bank.writing_questions VALUES (%s,%s,%s,%s)", (ids[0], slot, source_id, Jsonb(content)))


def test_existing_membership_cannot_be_replaced(local_set, bank):
    storage, run_id = local_set
    source = storage.publication_source_key(run_id, create=True)
    rows = storage.list_generated(run_id)
    bank.save_set(source, rows)
    before = bank.get_set(source)
    rows[0]["id"] += 100
    with pytest.raises(ValueError, match="문항 구성"):
        bank.save_set(source, rows)
    assert bank.get_set(source) == before


@pytest.mark.parametrize("pending_vocabulary", [False, True])
def test_startup_backs_up_and_applies_only_approved_migrations(bank, tmp_path, monkeypatch, pending_vocabulary):
    with psycopg.connect(bank.url) as c:
        c.execute("DROP TABLE topik_bank.writing_questions,topik_bank.writing_sets")
        c.execute("DELETE FROM topik_bank.schema_migrations WHERE version=%s", (writing_postgres.MIGRATION,))
        if pending_vocabulary:
            c.execute("DROP TABLE topik_bank.vocabulary_questions")
            c.execute("DELETE FROM topik_bank.schema_migrations WHERE version='017_vocabulary_questions'")
        config = {"data_directory": c.execute("SHOW data_directory").fetchone()[0]}
    backups = []
    monkeypatch.setattr(bank_migrations, "_local_config", lambda *args: config)

    def backup(*args, **kwargs):
        assert not bank.available()
        backups.append(kwargs["label"])
        return tmp_path / "backup.dump"

    monkeypatch.setattr(bank_migrations, "_backup_database", backup)
    versions, path = bank_migrations.migrate_local_bank(tmp_path, bank.url)
    assert versions == (["017_vocabulary_questions"] if pending_vocabulary else []) + [writing_postgres.MIGRATION]
    assert backups == ["018"] and path == tmp_path / "backup.dump"
    assert bank.available()
    with psycopg.connect(bank.url) as c:
        writing_postgres.apply_writing_migration(c)
        assert c.execute("SELECT count(*) FROM topik_bank.schema_migrations WHERE version=%s", (writing_postgres.MIGRATION,)).fetchone()[0] == 1


def test_failed_startup_migration_leaves_no_partial_tables_or_version(bank, tmp_path, monkeypatch):
    with psycopg.connect(bank.url) as c:
        c.execute("DROP TABLE topik_bank.writing_questions,topik_bank.writing_sets")
        c.execute("DELETE FROM topik_bank.schema_migrations WHERE version=%s", (writing_postgres.MIGRATION,))
        config = {"data_directory": c.execute("SHOW data_directory").fetchone()[0]}
    monkeypatch.setattr(bank_migrations, "_local_config", lambda *args: config)
    monkeypatch.setattr(bank_migrations, "_backup_database", lambda *args, **kwargs: tmp_path / "backup.dump")
    original = writing_postgres.apply_writing_migration

    def fail(c):
        original(c)
        raise RuntimeError("failed after DDL")

    monkeypatch.setattr(writing_postgres, "apply_writing_migration", fail)
    with pytest.raises(bank_migrations.StartupMigrationError):
        bank_migrations.migrate_local_bank(tmp_path, bank.url)
    with psycopg.connect(bank.url) as c:
        assert c.execute("SELECT to_regclass('topik_bank.writing_sets'),to_regclass('topik_bank.writing_questions')").fetchone() == (None, None)
        assert not c.execute("SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s", (writing_postgres.MIGRATION,)).fetchone()


def test_publication_ui_saves_latest_edits_and_retries(local_set, bank, monkeypatch):
    storage, run_id = local_set
    monkeypatch.setenv("DATABASE_URL", bank.url)
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("6. Hub 세트 저장").run()
    assert not app.exception and not app.error
    assert storage.publication_source_key(run_id) is None
    row = next(r for r in storage.list_generated(run_id) if r["question"]["type_slot"] == 51)
    passage = "미리보기 이후 수정 (㉠). 저장 (㉡)."
    storage.save_generated_edit(row["id"], {**row["question"], "passage": passage})
    app.button(key="writing-publish").click().run()
    assert not app.exception and not app.error
    assert any("한 세트로 저장" in s.value for s in app.success)
    source = storage.publication_source_key(run_id)
    saved = bank.get_set(source)
    assert saved["questions"][0]["content_json"]["passage"] == passage
    app.button(key="writing-publish").click().run()
    assert not app.exception and any("갱신" in s.value for s in app.success)
    assert bank.get_set(source)["set_id"] == saved["set_id"]


def test_publication_ui_handles_no_connection_and_incomplete_sets(local_set):
    storage, run_id = local_set
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("6. Hub 세트 저장").run()
    assert not app.exception
    assert any("연결 설정" in info.value for info in app.info)
    assert not any(b.key == "writing-publish" for b in app.button)
    with storage.connect() as c:
        c.execute("DELETE FROM generated_questions WHERE id=(SELECT MAX(id) FROM generated_questions)")
    app.run()
    assert not app.exception and app.warning
    assert not any(b.key == "writing-publish" for b in app.button)


def test_publication_refreshes_legacy_cached_storage(local_set, bank, monkeypatch):
    from topik_question_lab.common import writing_storage
    from topik_question_lab.common.storage import Storage

    class LegacyWritingStorage(Storage):
        def list_generated(self):
            return WritingStorage.list_generated(self)

    storage, run_id = local_set
    monkeypatch.setenv("DATABASE_URL", bank.url)
    monkeypatch.setattr(writing_storage, "WritingStorage", LegacyWritingStorage)
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert not app.exception

    # A running app keeps resource entries when the imported storage class changes.
    monkeypatch.setattr(writing_storage, "WritingStorage", WritingStorage)
    app.sidebar.radio(key="writing-stage").set_value("6. Hub 세트 저장").run()
    assert not app.exception and not app.error
    app.button(key="writing-publish").click().run()
    assert not app.exception and not app.error
    saved = bank.get_set(storage.publication_source_key(run_id))
    assert len(saved["questions"]) == 4
