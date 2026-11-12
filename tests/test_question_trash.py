from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import json
import sqlite3
import threading

import pytest
from streamlit.testing.v1 import AppTest

from test_direct_publication import seed_batch, change_question
from test_storage_overview import files
from topik_question_lab.common.storage import Storage
from topik_question_lab.common.listening_storage import ListeningStorage
from topik_question_lab.common.models import Review
from topik_question_lab.common.question_lifecycle import has_trash_column
from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog
from topik_question_lab.menus.registration.question_trash import QuestionTrash, trash_token
from topik_question_lab.menus.registration.registration_workflow import RegistrationWorkflow
from topik_question_lab.common.question_bank.storage_overview import load_overview


PAGE = Path(__file__).resolve().parents[1] / "topik_question_lab/menus/registration/postgres_publish_app.py"
META = {"level": 4, "difficulty": 0.0, "items": {}}


@pytest.fixture
def trash_bank(tmp_path, postgres_database_factory, monkeypatch):
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    seed_batch(tmp_path)
    seed_batch(tmp_path, "listening", approved=False)
    for path in (tmp_path / "data").rglob("*.db"):
        if path.name == "bulk_generation.db":
            continue
        with sqlite3.connect(path) as c:
            c.execute("UPDATE runs SET batch_id=NULL")
    monkeypatch.setenv("DATABASE_URL", url)
    return QuestionTrash(tmp_path, url)


def expected(service, keys):
    records = {r.candidate.source_key: r for r in collect_candidate_catalog(service.root, include_trashed=True).records}
    return {key: trash_token(records[key]) for key in keys}


def test_multiple_domains_restore_original_contents_and_review(trash_bank):
    t = trash_bank
    chosen = ["reading:grammar_blank:1", "reading:content_match_short:1", "listening:paired_21_22:1"]
    before = {r.candidate.source_key: asdict(r.candidate) for r in collect_candidate_catalog(t.root).records}
    result = t.apply(expected(t, chosen))
    assert result["complete"] and set(result["changed"]) == set(chosen)
    visible = {r.candidate.source_key for r in collect_candidate_catalog(t.root).records}
    assert len(visible) == 97 and not visible.intersection(chosen)
    all_records = collect_candidate_catalog(t.root, include_trashed=True)
    assert len(all_records.records) == 100
    assert all(not r.eligible for r in all_records.records if r.trashed_at)
    snap = load_overview(t.root, t.reader.target_url)
    assert sum(r["generated_questions"] for r in snap["inventory"]) == 97
    assert sum(r["trashed_questions"] for r in snap["inventory"]) == 3
    assert t.apply(expected(t, chosen), restore=True)["complete"]
    after = {r.candidate.source_key: asdict(r.candidate) for r in collect_candidate_catalog(t.root).records}
    assert before == after
    with t.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 0


def test_review_lists_and_stale_edit_exclude_trash(trash_bank):
    t = trash_bank
    stores = [(Storage(t.root / "data/types/grammar_blank.db"), "reading:grammar_blank:1"),
              (ListeningStorage(t.root / "data/listening/types/paired_21_22.db"), "listening:paired_21_22:1")]
    for store, key in stores:
        original = next(r for r in store.list_generated() if r["id"] == 1)
        t.apply(expected(t, [key]))
        assert 1 not in [r["id"] for r in store.list_generated()]
        with pytest.raises(ValueError, match="휴지통"):
            store.save_review(1, Review(approved=True))
        with pytest.raises(ValueError, match="휴지통"):
            store.save_generated_edit(1, original["question"])
        with pytest.raises(ValueError, match="휴지통"):
            store.save_validation(1, [])
        t.apply(expected(t, [key]), restore=True)
        restored = next(r for r in store.list_generated() if r["id"] == 1)
        assert restored["question"] == original["question"] and restored["review"] == original["review"]


def test_registered_or_batch_selection_changes_nothing(trash_bank):
    t = trash_bank
    workflow = RegistrationWorkflow(t.root, t.reader.target_url)
    registered = "reading:grammar_blank:1"
    workflow.execute(workflow.prepare("add", [registered], META))
    before = files(t.root)
    with pytest.raises(ValueError, match="등록된"):
        t.apply(expected(t, [registered, "reading:grammar_blank:2"]))
    assert files(t.root) == before
    bid = seed_batch(t.root)
    batch_key = next(r.candidate.source_key for r in collect_candidate_catalog(t.root).records if r.candidate.batch_id == bid)
    before = files(t.root)
    with pytest.raises(ValueError, match="일괄 생성"):
        t.apply(expected(t, [batch_key, "reading:grammar_blank:2"]))
    assert files(t.root) == before


def test_generation_receipt_is_protected_even_without_run_batch_id(trash_bank):
    t = trash_bank
    path = t.root / "data/types/grammar_blank.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE generation_receipts(batch_id TEXT,run_id INTEGER,question_ids_json TEXT)")
        c.execute("INSERT INTO generation_receipts VALUES ('old-batch',1,'[1,2]')")
    before = files(t.root)
    with pytest.raises(ValueError, match="일괄 생성 기록"):
        t.apply(expected(t, ["reading:grammar_blank:1"]))
    assert files(t.root) == before


def test_stale_selection_and_offline_db_cannot_dispose(trash_bank):
    t = trash_bank
    chosen = expected(t, ["reading:grammar_blank:1"])
    change_question(t.root, edit=lambda q: {**q, "explanation": "수정됨"})
    before = files(t.root)
    with pytest.raises(ValueError, match="바뀌었습니다"):
        t.apply(chosen)
    offline = QuestionTrash(t.root, "")
    with pytest.raises(ValueError):
        offline.apply(expected(t, ["reading:grammar_blank:1"]))
    assert files(t.root) == before


def test_trash_blocks_already_prepared_registration(trash_bank):
    t = trash_bank
    w = RegistrationWorkflow(t.root, t.reader.target_url)
    plan = w.prepare("add", ["reading:grammar_blank:1"], META)
    t.apply(expected(t, ["reading:grammar_blank:1"]))
    assert w.execute(plan)["status"] == "stale"
    with pytest.raises(ValueError):
        w.prepare("add", ["reading:grammar_blank:1"], META)


def test_concurrent_registration_and_trash_cannot_both_succeed(trash_bank):
    t = trash_bank
    w = RegistrationWorkflow(t.root, t.reader.target_url)
    key = "reading:grammar_blank:1"
    plan, tokens = w.prepare("add", [key], META), expected(t, [key])
    barrier = threading.Barrier(2)
    def discard():
        barrier.wait(timeout=10)
        try:
            return t.apply(tokens)["complete"]
        except ValueError:
            return False
    def register():
        barrier.wait(timeout=10)
        return w.execute(plan)["status"] == "succeeded"
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(discard), pool.submit(register)
        results = [a.result(timeout=30), b.result(timeout=30)]
    assert sum(results) == 1
    with t.reader.connect(True) as c:
        registered = c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0]
    trashed = next(r.trashed_at for r in collect_candidate_catalog(t.root, include_trashed=True).records if r.candidate.source_key == key)
    assert not (registered and trashed)


def test_backup_retains_trash_and_restore_can_recover(trash_bank):
    from topik_question_lab.maintenance.backup_service import create_backup, restore_backup, BackupOptions
    t = trash_bank
    key = "reading:grammar_blank:1"
    t.apply(expected(t, [key]))
    artifact = create_backup(t.root, BackupOptions(include_sources=False))
    destination = t.root / "restored"
    restore_backup(destination, artifact.data)
    restored = QuestionTrash(destination, t.reader.target_url)
    assert key not in {r.candidate.source_key for r in collect_candidate_catalog(destination).records}
    assert restored.apply(expected(restored, [key]), restore=True)["complete"]


def test_ui_multiple_selection_and_restore_without_implicit_changes(trash_bank):
    t = trash_bank
    before = files(t.root)
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    next(b for b in app.button if b.label == "미등록 문제 정리").click().run()
    assert not app.exception and files(t.root) == before
    next(b for b in app.button if b.label == "현재 목록 전체 선택").click().run()
    next(s for s in app.selectbox if s.key == "trash-cleanup-section").set_value("reading").run()
    assert not app.exception
    assert len(next(m for m in app.multiselect if m.label == "버릴 문제").value) == 50
    next(s for s in app.selectbox if s.key == "trash-cleanup-section").set_value("all").run()
    selected = ["reading:grammar_blank:1", "listening:paired_21_22:1"]
    next(m for m in app.multiselect if m.label == "버릴 문제").set_value(selected).run()
    assert files(t.root) == before
    next(b for b in app.button if b.label == "선택한 2문제 휴지통으로 이동").click().run()
    assert not app.exception
    assert any("2문제를 휴지통으로 이동" in s.value for s in app.success)
    next(b for b in app.button if b.label == "휴지통 보기 · 2문제").click().run()
    next(m for m in app.multiselect if m.label == "복원할 문제").set_value(selected).run()
    next(b for b in app.button if b.label == "선택한 2문제 복원하기").click().run()
    assert not app.exception
    assert len(collect_candidate_catalog(t.root).records) == 100


def test_partial_commit_is_reported_and_remaining_files_roll_back(trash_bank, monkeypatch):
    import topik_question_lab.menus.registration.question_trash as module
    t = trash_bank
    keys = ["reading:content_match_short:1", "reading:grammar_blank:1"]
    tokens = expected(t, keys)
    original = sqlite3.connect
    class FailCommit:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def commit(self):
            raise sqlite3.OperationalError("synthetic disk failure")
    def connect(path, *args, **kwargs):
        connection = original(path, *args, **kwargs)
        return FailCommit(connection) if "grammar_blank.db?mode=rw" in str(path) else connection
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    result = t.apply(tokens)
    assert not result["complete"] and result["changed"] == keys[:1]
    states = {r.candidate.source_key: r.trashed_at for r in collect_candidate_catalog(t.root, include_trashed=True).records}
    assert states[keys[0]] and states[keys[1]] is None
    with original(t.root / "data/types/grammar_blank.db") as c:
        assert not has_trash_column(c)
