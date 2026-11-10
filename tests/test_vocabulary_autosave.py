from copy import deepcopy
import threading

import psycopg
import pytest

from test_vocabulary_questions import CONFIG, FakeBank, bank, bundle, finish, ready_task, result, review, successful_caller
from topik_question_lab.step_03_generation.vocabulary_bulk import VocabularyRunner
from topik_question_lab.step_06_publication.bank_transfer import TransferError


def test_accepted_word_is_saved_while_next_word_is_still_generating(tmp_path):
    fake = FakeBank()
    fake.snapshots[2] = deepcopy(fake.snapshots[1])
    fake.snapshots[2]["word"]["vocabulary_id"] = 2
    calls, entered, release = [], threading.Event(), threading.Event()
    def invoke(**kwargs):
        calls.append(kwargs["operation"])
        if len(calls) == 3:
            entered.set()
            assert release.wait(10)
        return result(review(bundle()) if kwargs["operation"] == "vocabulary_review" else bundle(), kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke, lambda _: fake)
    batch_id = runner.start("fake", snapshots=[fake.snapshot(1), fake.snapshot(2)], config={**CONFIG, "concurrency": 1})
    try:
        assert entered.wait(5)
        assert runner.active and runner.store.tasks(batch_id)[0]["status"] == "saved"
        assert len(fake.commits) == 1
    finally:
        release.set()
        finish(runner)
    assert all(t["status"] == "saved" for t in runner.store.tasks(batch_id))


def test_postgres_save_failure_resets_and_next_run_starts_with_generation(bank, tmp_path, monkeypatch):
    calls = []
    runner = VocabularyRunner(tmp_path, successful_caller(calls), lambda _: bank)
    original = bank.save
    def fail(*args):
        raise TransferError("simulated connection failure before commit")
    monkeypatch.setattr(bank, "save", fail)
    batch_id = runner.start(bank.url, snapshots=bank.select(), config=CONFIG)
    finish(runner)
    failed = runner.store.tasks(batch_id)[0]
    assert failed["stage"] == "generate" and failed["status"] == "pending"
    assert failed["attempts"] == failed["repair_count"] == 0
    assert failed["checkpoint"]["last_attempt"]["stage"] == "save"
    assert failed["checkpoint"]["last_attempt"]["reviewed_hash"]
    assert "reviewed_hash" not in failed["checkpoint"]
    assert bank.progress() == {"total": 1, "completed": 0, "remaining": 1}
    monkeypatch.setattr(bank, "save", original)
    next_id = runner.start(bank.url, snapshots=bank.select(), config=CONFIG)
    finish(runner)
    assert calls == ["generation", "vocabulary_review"] * 2
    assert runner.store.tasks(next_id)[0]["status"] == "saved"
    assert bank.select() == [] and bank.progress() == {"total": 1, "completed": 1, "remaining": 0}
    assert runner.store.task(failed["generation_id"]) == failed


def test_lost_postgres_commit_response_is_confirmed_without_second_write(bank, tmp_path, monkeypatch):
    original, saves, calls = bank.save, [], []
    def committed_then_lost(task, config, database_key):
        saves.append(task["generation_id"])
        original(task, config, database_key)
        raise TransferError("simulated lost commit response")
    monkeypatch.setattr(bank, "save", committed_then_lost)
    runner = VocabularyRunner(tmp_path, successful_caller(calls), lambda _: bank)
    batch_id = runner.start(bank.url, snapshots=bank.select(), config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "saved" and len(calls) == 2 and saves == [task["generation_id"]]
    assert bank.confirm_saved(task, CONFIG, bank.identity())
    assert len(bank.snapshot(1)["existing"]) == 7 and bank.select() == []


def test_auto_save_conflict_keeps_other_users_edit_and_returns_to_initial_state(bank, tmp_path):
    bank.save(ready_task(bank), CONFIG, bank.identity())
    original = bank.snapshot(1)
    calls = []
    def invoke(**kwargs):
        calls.append(kwargs["operation"])
        if kwargs["operation"] == "vocabulary_review":
            with psycopg.connect(bank.url) as c:
                c.execute("UPDATE topik_bank.vocabulary_questions SET explanation='직접 수정한 최신 해설' WHERE position=1")
            return result(review(bundle()), kwargs["operation"])
        return result(bundle())
    runner = VocabularyRunner(tmp_path, invoke)
    batch_id = runner.start(bank.url, snapshots=[original], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "pending" and task["stage"] == "generate"
    assert "기존 문항" in task["error"] and len(calls) == 2
    assert bank.snapshot(1)["existing"][0]["explanation"] == "직접 수정한 최신 해설"
    assert bank.snapshot(1)["existing"][1:] == original["existing"][1:]


@pytest.mark.parametrize("mutation", ["delete_question", "change_source", "change_generation", "invalid_choice"])
def test_progress_and_default_selection_agree_on_incomplete_words(bank, mutation):
    assert bank.progress() == {"total": 1, "completed": 0, "remaining": 1}
    bank.save(ready_task(bank), CONFIG, bank.identity())
    assert bank.progress() == {"total": 1, "completed": 1, "remaining": 0}
    assert bank.select() == []
    sql = {
        "delete_question": "DELETE FROM topik_bank.vocabulary_questions WHERE position=1",
        "change_source": "UPDATE topik_bank.vocabulary SET english='defer' WHERE vocabulary_id=1",
        "change_generation": "UPDATE topik_bank.vocabulary_questions SET generation_id='00000000-0000-0000-0000-000000000001' WHERE position=1",
        "invalid_choice": "UPDATE topik_bank.vocabulary_questions SET choices='[\"same\",\"same\",\"same\",\"same\"]'::jsonb WHERE position=1",
    }[mutation]
    with psycopg.connect(bank.url) as c:
        c.execute(sql)
    assert bank.progress() == {"total": 1, "completed": 0, "remaining": 1}
    assert [s["word"]["vocabulary_id"] for s in bank.select()] == [1]


def test_progress_counts_are_global_and_ignore_optional_relation_omission(bank):
    bank.save(ready_task(bank, bundle(relation=False)), CONFIG, bank.identity())
    with psycopg.connect(bank.url) as c:
        c.execute("""INSERT INTO topik_bank.vocabulary(korean,part_of_speech,english,vietnamese,japanese,
            example_korean,example_english,topik_level)
            SELECT '추가 단어','동사',english,vietnamese,japanese,example_korean,example_english,4
            FROM topik_bank.vocabulary WHERE vocabulary_id=1""")
    assert bank.progress() == {"total": 2, "completed": 1, "remaining": 1}
    assert bank.select(level=3) == []
    assert len(bank.select(level=4, query="추가")) == 1
    assert bank.progress() == {"total": 2, "completed": 1, "remaining": 1}


def test_missing_question_schema_and_empty_vocabulary_progress_are_read_only(bank):
    with psycopg.connect(bank.url) as c:
        c.execute("DROP TABLE topik_bank.vocabulary_questions")
    assert bank.progress() == {"total": 1, "completed": 0, "remaining": 1}
    with psycopg.connect(bank.url) as c:
        assert c.execute("SELECT to_regclass('topik_bank.vocabulary_questions')").fetchone()[0] is None
        c.execute("DELETE FROM topik_bank.vocabulary")
    assert bank.progress() == {"total": 0, "completed": 0, "remaining": 0}
