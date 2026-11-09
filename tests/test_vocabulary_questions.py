from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import uuid

import psycopg
from psycopg.types.json import Jsonb
import pytest

from topik_question_lab.common.models import ProviderResult
from topik_question_lab.common.vocabulary_models import digest, source_snapshot, validate_bundle
from topik_question_lab.common.vocabulary_storage import VocabularyStore
from topik_question_lab.step_03_generation.vocabulary_bulk import VocabularyRunner
from topik_question_lab.step_04_review.vocabulary_validation import apply_review
from topik_question_lab.step_06_publication.vocabulary_questions import (
    MIGRATION, VocabularyQuestionBank, apply_question_migration, question_status,
)
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR


WORD = source_snapshot({"vocabulary_id": 1, "korean": "미루다", "part_of_speech": "동사", "english": "postpone",
                        "vietnamese": "hoãn", "japanese": "延期する", "example_korean": "회의를 미뤘어요.",
                        "example_english": "I postponed the meeting.", "topik_level": 3})
CONFIG = {"provider": "test", "model": "test-model", "concurrency": 2}


def bundle(*, conjugation=True, relation=True):
    questions = []
    for position, stem in enumerate(("비가 와서 운동회를 ( ).", "몸이 아파서 약속을 ( ).", "시간이 없어서 숙제를 ( )."), 1):
        questions.append({"position": position, "question_type": "sentence_blank", "instruction": "알맞은 말을 고르세요.",
                          "stem": stem, "choices": ["지켰어요", "미뤘어요", "잊었어요", "준비했어요"], "correct_answer": 2,
                          "accepted_answers": ["미뤘어요", "미루었어요"], "explanation": "예정된 일을 나중으로 늦춘다는 뜻입니다."})
    if conjugation:
        for position, tense in enumerate(("현재", "과거", "미래"), 4):
            questions.append({"position": position, "question_type": "conjugation", "instruction": "해요체로 바꾸세요.",
                              "stem": f"‘미루다’를 {tense} 표현으로 바꾸세요.", "choices": ["미뤄요", "미뤘어요", "미룰 거예요", "미뤄서"],
                              "correct_answer": position - 3, "accepted_answers": [], "explanation": f"{tense} 표현입니다."})
    if relation:
        questions.append({"position": 7, "question_type": "synonym", "instruction": "비슷한 뜻을 고르세요.",
                          "stem": "‘회의를 미루다’의 ‘미루다’와 비슷한 뜻은?", "choices": ["연기하다", "참석하다", "시작하다", "취소하다"],
                          "correct_answer": 1, "accepted_answers": [], "explanation": "일정을 늦춘다는 뜻입니다."})
    return {"target_meaning": "예정된 일을 나중으로 늦추다", "questions": questions,
            "skipped_reason": "" if relation else "문맥에서 적절한 관계를 찾기 어렵습니다."}


def review(candidate, *, failed=()):
    return {"issues": [], "questions": [{"position": q["position"], "issues": ["복수 정답 가능"] if q["position"] in failed else []}
                                         for q in candidate["questions"]]}


def result(payload, operation="generation", **kwargs):
    return ProviderResult(provider="test", model="test-model", operation=operation,
                          parsed_json=payload, raw_response=json.dumps(payload, ensure_ascii=False), **kwargs)


class FakeBank:
    def __init__(self):
        self.snapshots = {1: {"word": deepcopy(WORD), "existing": [], "existing_hash": digest([])}}
        self.commits = {}
        self.fail_after_commit = False
        self.url = "fake"

    def identity(self):
        return digest("test-bank")

    def snapshot(self, vocabulary_id):
        return deepcopy(self.snapshots[vocabulary_id])

    def save(self, task, config, database_key):
        self.commits.setdefault(task["generation_id"], deepcopy(task["checkpoint"]["candidate"]))
        if self.fail_after_commit:
            self.fail_after_commit = False
            from topik_question_lab.step_06_publication.bank_transfer import TransferError
            raise TransferError("connection lost")


def successful_caller(calls):
    def invoke(**kwargs):
        calls.append(kwargs["operation"])
        candidate = bundle()
        return result(review(candidate) if kwargs["operation"] == "vocabulary_review" else candidate, kwargs["operation"])
    return invoke


def finish(runner):
    runner.thread.join(15)
    assert not runner.active


def ready_task(bank, candidate=None):
    snapshot = bank.snapshot(1)
    candidate = validate_bundle(candidate or bundle(), snapshot["word"]).model_dump()
    return {"generation_id": str(uuid.uuid4()), "vocabulary_id": 1, "snapshot": snapshot,
            "source_hash": digest(snapshot["word"]), "checkpoint": {"candidate": candidate,
                "review": {"passed": True}, "reviewed_hash": digest(candidate)}}


@pytest.mark.parametrize("conjugation,relation,count", [(True, True, 7), (True, False, 6), (False, True, 4), (False, False, 3)])
def test_counts_and_subjective_answers(conjugation, relation, count):
    word = {**WORD, "part_of_speech": "동사" if conjugation else "명사"}
    value = validate_bundle(bundle(conjugation=conjugation, relation=relation), word)
    assert len(value.questions) == count
    assert value.questions[0].accepted_answers == ["미뤘어요", "미루었어요"]


@pytest.mark.parametrize("change", [
    lambda b: b["questions"].pop(1),
    lambda b: b["questions"][0].update(accepted_answers=[]),
    lambda b: b["questions"][0].update(choices=["미뤘어요"] * 4),
    lambda b: b["questions"][0].update(correct_answer=5),
    lambda b: b["questions"][0].update(stem="빈칸 없음"),
    lambda b: b["questions"][0].update(position=4),
    lambda b: b["questions"][0].update(accepted_answers=["미뤘어요", "지켰어요"]),
    lambda b: b["questions"][1].update(stem=b["questions"][0]["stem"]),
])
def test_invalid_bundles_are_rejected(change):
    candidate = bundle()
    change(candidate)
    with pytest.raises(ValueError):
        validate_bundle(candidate, WORD)


def test_unknown_pos_and_optional_review():
    with pytest.raises(ValueError, match="품사"):
        validate_bundle(bundle(), {**WORD, "part_of_speech": ""})
    candidate, verdict, failed = apply_review(review(bundle(), failed=(7,)), bundle(), WORD)
    assert verdict["passed"] and not failed
    assert len(candidate["questions"]) == 6 and candidate["skipped_reason"]
    with pytest.raises(ValueError):
        apply_review({"issues": [], "questions": []}, bundle(), WORD)


def test_sqlite_constraints_and_backup_restore(tmp_path):
    from topik_question_lab.maintenance.backup_service import create_backup, restore_backup
    root = tmp_path / "source"
    store = VocabularyStore(root)
    batch_id = store.create("bank", CONFIG, [FakeBank().snapshot(1)])
    task = store.tasks(batch_id)[0]
    for sql, params in [
        ("UPDATE vocabulary_tasks SET batch_id='missing'", ()),
        ("UPDATE vocabulary_tasks SET repair_count=2", ()),
        ("UPDATE vocabulary_tasks SET checkpoint_json='[]'", ()),
        ("DELETE FROM vocabulary_batches", ()),
    ]:
        with pytest.raises(sqlite3.IntegrityError), store.connect() as c:
            c.execute(sql, params)
    store.update(task["generation_id"], status="processing", stage="review", checkpoint={"candidate": bundle()})
    store.set_batch(batch_id, "running")
    artifact = create_backup(root)
    destination = tmp_path / "restored"
    destination.mkdir()
    restore_backup(destination, artifact.data)
    restored = VocabularyStore(destination)
    assert restored.batch(batch_id)["status"] == "paused"
    recovered = restored.task(task["generation_id"])
    assert recovered["status"] == "pending" and recovered["stage"] == "review"
    assert recovered["checkpoint"]["candidate"] == bundle()


def test_generate_review_edit_revalidate_and_save_recovery(tmp_path):
    bank, calls = FakeBank(), []
    runner = VocabularyRunner(tmp_path, successful_caller(calls), lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "ready" and calls == ["generation", "vocabulary_review"]
    edited = deepcopy(task["checkpoint"]["candidate"])
    edited["questions"][0]["explanation"] += " 확인했습니다."
    runner.edit(task["generation_id"], edited)
    assert "reviewed_hash" not in runner.store.task(task["generation_id"])["checkpoint"]
    runner.start("fake", batch_id=batch_id)
    finish(runner)
    assert calls == ["generation", "vocabulary_review", "vocabulary_review"]
    bank.fail_after_commit = True
    assert runner.save_ready("fake", batch_id)[0][1] == "failed"
    assert runner.save_ready("fake", batch_id)[0][1] == "saved"
    assert len(bank.commits) == 1
    assert runner.store.batch(batch_id)["status"] == "completed"


def test_repairs_only_failed_positions_once(tmp_path):
    bank, operations = FakeBank(), []
    def invoke(**kwargs):
        operations.append(kwargs["operation"])
        if kwargs["operation"] == "vocabulary_review":
            return result(review(bundle(), failed=(2,)), kwargs["operation"])
        if len(operations) == 1:
            return result(bundle())
        assert "수정할 위치: [2]" in kwargs["user_prompt"]
        q = bundle()["questions"][1]
        q["stem"] = "일이 생겨서 만남을 ( )."
        return result({"questions": [q]})
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "needs_attention" and task["repair_count"] == 1
    assert operations == ["generation", "vocabulary_review", "generation", "vocabulary_review"]
    assert task["checkpoint"]["candidate"]["questions"][0] == bundle()["questions"][0]


def test_missing_subjective_answers_get_specific_targeted_repair(tmp_path):
    bank, calls = FakeBank(), []
    bad = bundle()
    for q in bad["questions"][:3]:
        q["accepted_answers"] = []
    def invoke(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return result(bad)
        if kwargs["operation"] == "generation":
            assert "수정할 위치: [1, 2, 3]" in kwargs["user_prompt"]
            assert "1번: 정답 보기를 주관식 허용 답안에 포함하세요." in kwargs["user_prompt"]
            assert "sentence_blank의 accepted_answers는 필수" in kwargs["system_prompt"]
            return result({"questions": bundle()["questions"][:3]})
        return result(review(bundle()), kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "ready"
    assert [call["operation"] for call in calls] == ["generation", "generation", "vocabulary_review"]
    assert task["checkpoint"]["candidate"]["questions"][3:] == bundle()["questions"][3:]


def test_legacy_validation_failure_resumes_from_candidate_with_updated_prompt(tmp_path):
    from topik_question_lab.step_02_planning.vocabulary_prompts import prompt_config, PROMPT_VERSION
    from topik_question_lab.step_04_review.vocabulary_validation import validation_retry_details
    bank, calls = FakeBank(), []
    def invoke(**kwargs):
        calls.append(kwargs)
        if kwargs["operation"] == "generation":
            assert "sentence_blank의 accepted_answers는 필수" in kwargs["system_prompt"]
            assert "수정할 위치: [1, 2, 3]" in kwargs["user_prompt"]
            return result({"questions": bundle()["questions"][:3]})
        return result(review(bundle()), kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    original_config = {**CONFIG, **prompt_config(), "prompt_version": "vocabulary-1", "system": "old prompt"}
    for key in ("generation_prompt", "repair_prompt", "prompt_customized"):
        original_config.pop(key)
    batch_id = runner.store.create(bank.identity(), original_config, [bank.snapshot(1)])
    task = runner.store.tasks(batch_id)[0]
    bad = bundle()
    for q in bad["questions"][:3]:
        q["accepted_answers"] = []
    history = [{"stage": "generate", "result": result(bad).model_dump(mode="json")}]
    runner.store.update(task["generation_id"], stage="repair", status="needs_attention", repair_count=1,
                        checkpoint={"candidate": bad, "calls": history})
    assert validation_retry_details(runner.store.task(task["generation_id"]))[1] == [1, 2, 3]
    runner.start("fake", batch_id=batch_id)
    finish(runner)
    after = runner.store.task(task["generation_id"])
    assert after["status"] == "ready" and after["repair_count"] == 1
    assert len(calls) == 2 and after["checkpoint"]["calls"][0] == history[0]
    assert after["checkpoint"]["prompt_override"]["prompt_version"] == PROMPT_VERSION
    assert runner.store.batch(batch_id)["config"] == original_config


def test_missing_answers_are_required_in_schema_and_remain_invalid():
    from topik_question_lab.common.vocabulary_models import VocabularyQuestion
    schema = VocabularyQuestion.model_json_schema()
    assert "accepted_answers" in schema["required"]
    bad = bundle()
    del bad["questions"][0]["accepted_answers"]
    with pytest.raises(ValueError):
        validate_bundle(bad, WORD)


def test_custom_prompts_are_snapshotted_sent_and_retained_on_resume(tmp_path):
    from topik_question_lab.step_02_planning.vocabulary_prompts import prompt_config, build_prompt
    bank, calls = FakeBank(), []
    custom = {"system": "사용자 공통 지침", "generation_prompt": "생성 지침 CUSTOM",
              "review_prompt": "검수 지침 CUSTOM", "repair_prompt": "수정 지침 CUSTOM"}
    bad = bundle()
    bad["questions"][0]["accepted_answers"] = []
    def invoke(**kwargs):
        calls.append(kwargs)
        if kwargs["operation"] == "vocabulary_review":
            return result(review(bundle()), kwargs["operation"])
        if len(calls) <= 2:
            return result(bad if len(calls) == 1 else {"questions": bad["questions"][:1]})
        return result({"questions": bundle()["questions"][:1]})
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config={**CONFIG, **custom})
    finish(runner)
    assert runner.store.tasks(batch_id)[0]["status"] == "needs_attention"
    saved = runner.store.batch(batch_id)["config"]
    assert saved["prompt_customized"] and all(saved[key] == value for key, value in custom.items())
    assert calls[0]["user_prompt"] == build_prompt("generate", WORD, {}, prompt_config(custom))
    assert calls[1]["user_prompt"].startswith(custom["repair_prompt"])
    runner.start("fake", batch_id=batch_id)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "ready"
    assert calls[2]["user_prompt"].startswith(custom["repair_prompt"])
    assert calls[3]["user_prompt"].startswith(custom["review_prompt"])
    for recorded, actual in zip(task["checkpoint"]["calls"], calls):
        assert recorded["system"] == actual["system_prompt"] == custom["system"]
        assert recorded["prompt"] == actual["user_prompt"]
    assert runner.store.batch(batch_id)["config"] == saved


@pytest.mark.parametrize("field", ["system", "generation_prompt", "review_prompt", "repair_prompt"])
def test_empty_custom_prompt_cannot_start_a_batch(tmp_path, field):
    bank = FakeBank()
    runner = VocabularyRunner(tmp_path, successful_caller([]), lambda _: bank)
    with pytest.raises(ValueError, match="입력"):
        runner.start("fake", snapshots=[bank.snapshot(1)], config={**CONFIG, field: "  "})
    assert runner.store.batches(bank.identity()) == []


def test_malformed_review_retries_review_only(tmp_path):
    bank, calls = FakeBank(), []
    def invoke(**kwargs):
        calls.append(kwargs["operation"])
        if len(calls) == 2:
            return result({"issues": [], "questions": []}, kwargs["operation"])
        return result(review(bundle()) if kwargs["operation"] == "vocabulary_review" else bundle(), kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    assert runner.store.tasks(batch_id)[0]["status"] == "failed"
    runner.start("fake", batch_id=batch_id)
    finish(runner)
    assert runner.store.tasks(batch_id)[0]["status"] == "ready"
    assert calls == ["generation", "vocabulary_review", "vocabulary_review"]


def test_pause_keeps_returned_response_and_lock_blocks_second_runner(tmp_path):
    bank, calls = FakeBank(), []
    entered, release = threading.Event(), threading.Event()
    def invoke(**kwargs):
        calls.append(kwargs["operation"])
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return result(review(bundle()) if kwargs["operation"] == "vocabulary_review" else bundle(), kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    try:
        assert entered.wait(5)
        second = VocabularyRunner(tmp_path, invoke, lambda _: bank)
        with pytest.raises(RuntimeError):
            second.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
        runner.stop()
    finally:
        release.set()
        finish(runner)
    assert runner.store.batch(batch_id)["status"] == "paused"
    runner.start("fake", batch_id=batch_id)
    finish(runner)
    assert calls == ["generation", "vocabulary_review"]


def test_resume_interprets_persisted_response_without_recalling_model(tmp_path):
    bank, calls = FakeBank(), []
    runner = VocabularyRunner(tmp_path, successful_caller(calls), lambda _: bank)
    from topik_question_lab.step_02_planning.vocabulary_prompts import prompt_config
    batch_id = runner.store.create(bank.identity(), {**CONFIG, **prompt_config()}, [bank.snapshot(1)])
    task = runner.store.tasks(batch_id)[0]
    runner.store.update(task["generation_id"], status="processing", attempts=1,
                        checkpoint={"pending_response": result(bundle()).model_dump(mode="json")})
    runner.start("fake", batch_id=batch_id)
    finish(runner)
    assert calls == ["vocabulary_review"]


def test_changed_source_stops_before_model_request(tmp_path):
    bank, calls = FakeBank(), []
    snapshot = bank.snapshot(1)
    bank.snapshots[1]["word"]["english"] = "defer"
    runner = VocabularyRunner(tmp_path, successful_caller(calls), lambda _: bank)
    batch_id = runner.start("fake", snapshots=[snapshot], config=CONFIG)
    finish(runner)
    assert not calls and runner.store.tasks(batch_id)[0]["status"] == "needs_attention"


def test_transient_retries_are_bounded_and_recorded(tmp_path, monkeypatch):
    from topik_question_lab.step_03_generation.vocabulary_bulk import RateGate
    monkeypatch.setattr(RateGate, "wait", lambda *args: None)
    bank, calls = FakeBank(), []
    def invoke(**kwargs):
        calls.append(kwargs)
        return result(None, error="HTTP 429", error_retryable=True, error_retry_after=1)
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert len(calls) == 3 and task["attempts"] == 3 and task["status"] == "failed"
    assert len(task["checkpoint"]["calls"]) == 3


def test_authentication_failure_pauses_batch_without_starting_remaining_words(tmp_path):
    bank, calls = FakeBank(), []
    bank.snapshots[2] = deepcopy(bank.snapshots[1])
    bank.snapshots[2]["word"]["vocabulary_id"] = 2
    def invoke(**kwargs):
        calls.append(kwargs)
        return result(None, error="HTTP 401")
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(1), bank.snapshot(2)], config={**CONFIG, "concurrency": 1})
    finish(runner)
    assert len(calls) == 1 and runner.store.batch(batch_id)["status"] == "paused"
    assert runner.store.tasks(batch_id)[1]["status"] == "pending"


def test_bounded_parallelism(tmp_path):
    bank = FakeBank()
    for word_id in (2, 3):
        bank.snapshots[word_id] = deepcopy(bank.snapshots[1])
        bank.snapshots[word_id]["word"]["vocabulary_id"] = word_id
    lock = threading.Lock()
    both_started, release = threading.Event(), threading.Event()
    active, maximum = 0, 0
    def invoke(**kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
        assert release.wait(10)
        with lock:
            active -= 1
        return result(review(bundle()) if kwargs["operation"] == "vocabulary_review" else bundle(), kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke, lambda _: bank)
    batch_id = runner.start("fake", snapshots=[bank.snapshot(i) for i in (1, 2, 3)], config=CONFIG)
    try:
        assert both_started.wait(5)
        assert sum(t["attempts"] > 0 for t in runner.store.tasks(batch_id)) == 2
    finally:
        release.set()
        finish(runner)
    assert maximum == 2
    assert all(t["status"] == "ready" for t in runner.store.tasks(batch_id))


def test_cached_runner_recovers_backup_without_word_tables(tmp_path):
    runner = VocabularyRunner(tmp_path, bank_factory=lambda _: FakeBank())
    with runner.store.connect() as c:
        c.execute("DROP TABLE vocabulary_tasks")
        c.execute("DROP TABLE vocabulary_batches")
    runner.recover_idle()
    assert runner.store.batches("missing") == []


def test_vocabulary_review_uses_existing_client_with_bounded_timeout(monkeypatch):
    from types import SimpleNamespace
    import openai
    from topik_question_lab.step_03_generation.providers import call_provider
    recorded = {}
    def client(**kwargs):
        recorded.update(kwargs)
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"questions": []}'))], usage=None)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
    monkeypatch.setenv("CHATKHU_API_KEY", "test-key")
    monkeypatch.setattr(openai, "OpenAI", client)
    value = call_provider("test", "test-model", "vocabulary_review", "system", "user", managed_retries=True, request_timeout=120)
    assert not value.error and value.operation == "vocabulary_review"
    assert recorded["max_retries"] == 0 and recorded["timeout"] == 120


@pytest.fixture
def bank(postgres_database_factory):
    url = postgres_database_factory()
    with psycopg.connect(url) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
        c.execute((MIGRATION_DIR / "012_vocabulary.sql").read_text(encoding="utf-8"))
        fields = [key for key in WORD if key not in {"vocabulary_id", "chinese_simplified", "indonesian"}]
        c.execute(f"INSERT INTO topik_bank.vocabulary({','.join(fields)}) VALUES ({','.join(['%s']*len(fields))})", [WORD[key] for key in fields])
        apply_question_migration(c)
    return VocabularyQuestionBank(url)


def test_postgres_atomic_save_idempotency_conflicts_and_optional_removal(bank):
    task = ready_task(bank)
    assert bank.save(task, CONFIG, bank.identity()) == "saved"
    saved = bank.snapshot(1)
    assert len(saved["existing"]) == 7 and question_status(saved) == "ready"
    assert bank.save(task, CONFIG, bank.identity()) == "reconciled"
    replacement = ready_task(bank, bundle(relation=False))
    bank.save(replacement, CONFIG, bank.identity())
    assert len(bank.snapshot(1)["existing"]) == 6
    assert bank.snapshot(1)["existing"][0]["question_id"] == saved["existing"][0]["question_id"]
    with pytest.raises(ValueError, match="기존 문항"):
        bank.save(task, CONFIG, bank.identity())
    stale = ready_task(bank)
    with psycopg.connect(bank.url) as c:
        c.execute("UPDATE topik_bank.vocabulary SET english='defer' WHERE vocabulary_id=1")
    with pytest.raises(ValueError, match="원본"):
        bank.save(stale, CONFIG, bank.identity())
    assert question_status(bank.snapshot(1)) == "stale"
    assert len(bank.select(mode="stale")) == 1


def test_postgres_constraints_and_migration_idempotency(bank):
    task = ready_task(bank)
    bank.save(task, CONFIG, bank.identity())
    with psycopg.connect(bank.url) as c:
        apply_question_migration(c)
        assert c.execute("SELECT count(*) FROM topik_bank.schema_migrations WHERE version=%s", (MIGRATION,)).fetchone()[0] == 1
    for sql in (
        "UPDATE topik_bank.vocabulary_questions SET choices='[]'::jsonb WHERE position=1",
        "UPDATE topik_bank.vocabulary_questions SET choices='{}'::jsonb WHERE position=1",
        "UPDATE topik_bank.vocabulary_questions SET correct_answer=5 WHERE position=1",
        "UPDATE topik_bank.vocabulary_questions SET accepted_answers='[]'::jsonb WHERE position=1",
        "UPDATE topik_bank.vocabulary_questions SET position=4 WHERE position=1",
        "UPDATE topik_bank.vocabulary_questions SET vocabulary_id=999",
        "UPDATE topik_bank.vocabulary_questions SET instruction=NULL WHERE position=1",
        "UPDATE topik_bank.vocabulary_questions SET source_hash='bad' WHERE position=1",
        "DELETE FROM topik_bank.vocabulary WHERE vocabulary_id=1",
    ):
        with pytest.raises(psycopg.IntegrityError), psycopg.connect(bank.url) as c:
            c.execute(sql)
    assert len(bank.snapshot(1)["existing"]) == 7


def test_postgres_whole_word_rolls_back_on_database_failure(bank):
    task = ready_task(bank)
    with psycopg.connect(bank.url) as c:
        c.execute("ALTER TABLE topik_bank.vocabulary_questions ADD CHECK(position<>3)")
    with pytest.raises(ValueError):
        bank.save(task, CONFIG, bank.identity())
    assert bank.snapshot(1)["existing"] == []


def test_postgres_readback_mismatch_rolls_back_the_word(bank):
    with psycopg.connect(bank.url) as c:
        c.execute("""CREATE FUNCTION topik_bank.rewrite_explanation() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN NEW.explanation := 'changed by trigger'; RETURN NEW; END $$""")
        c.execute("""CREATE TRIGGER rewrite_explanation BEFORE INSERT ON topik_bank.vocabulary_questions
            FOR EACH ROW EXECUTE FUNCTION topik_bank.rewrite_explanation()""")
    with pytest.raises(ValueError, match="일치하지 않아"):
        bank.save(ready_task(bank), CONFIG, bank.identity())
    assert not bank.snapshot(1)["existing"]


def test_postgres_rejects_unreviewed_edit_and_wrong_database(bank):
    task = ready_task(bank)
    task["checkpoint"]["candidate"]["questions"][0]["explanation"] += " 수정"
    with pytest.raises(ValueError, match="검수"):
        bank.save(task, CONFIG, bank.identity())
    with pytest.raises(ValueError, match="DB"):
        bank.save(ready_task(bank), CONFIG, "different")


def test_postgres_selection_and_edit_conflict(bank):
    assert bank.select(query="미루", mode="missing")[0]["word"] == WORD
    assert not bank.select(query="%", mode="missing")
    bank.save(ready_task(bank), CONFIG, bank.identity())
    assert not bank.select(mode="missing")
    assert len(bank.select(mode="regenerate", ids=[1])) == 1
    task = ready_task(bank)
    with psycopg.connect(bank.url) as c:
        c.execute("UPDATE topik_bank.vocabulary_questions SET explanation='사용자 수정' WHERE position=1")
    with pytest.raises(ValueError, match="기존 문항"):
        bank.save(task, CONFIG, bank.identity())


def test_017_startup_migration_backs_up_and_rolls_back_on_error(bank, tmp_path, monkeypatch):
    from topik_question_lab.maintenance import bank_migrations as migration
    from topik_question_lab.step_06_publication import vocabulary_questions as questions
    with psycopg.connect(bank.url) as c:
        c.execute("DROP TABLE topik_bank.vocabulary_questions")
        c.execute("DELETE FROM topik_bank.schema_migrations WHERE version=%s", (MIGRATION,))
        directory = c.execute("SHOW data_directory").fetchone()[0]
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    (reviewed / f"{MIGRATION}.sql").write_text((MIGRATION_DIR / f"{MIGRATION}.sql").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(migration, "MIGRATION_DIR", reviewed)
    monkeypatch.setattr(questions, "MIGRATION_DIR", reviewed)
    monkeypatch.setattr(migration, "_local_config", lambda *args: {"data_directory": directory})
    backups = []
    def backup(*args, **kwargs):
        backups.append(kwargs["label"])
        return tmp_path / "backup.dump"
    monkeypatch.setattr(migration, "_backup_database", backup)
    original = questions.apply_question_migration
    def fail(c):
        original(c)
        raise RuntimeError("failed after DDL")
    monkeypatch.setattr(questions, "apply_question_migration", fail)
    with pytest.raises(migration.StartupMigrationError):
        migration.migrate_local_bank(tmp_path, bank.url)
    assert not bank.available()
    monkeypatch.setattr(questions, "apply_question_migration", original)
    versions, saved = migration.migrate_local_bank(tmp_path, bank.url)
    assert versions == [MIGRATION] and saved.name == "backup.dump" and backups == ["017", "017"]
    assert bank.available()


def test_exercise_page_batch_preview_and_subjective_mode(bank, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from topik_question_lab.ui import vocabulary_questions_ui as ui
    calls = []
    runner = VocabularyRunner(tmp_path, successful_caller(calls))
    batch_id = runner.start(bank.url, snapshots=[bank.snapshot(1)], config=CONFIG)
    finish(runner)
    monkeypatch.setenv("DATABASE_URL", bank.url)
    monkeypatch.setattr(ui, "get_vocabulary_runner", lambda _: runner)
    page = Path(__file__).resolve().parents[1] / "topik_question_lab/ui/vocabulary_questions_app.py"
    app = AppTest.from_file(str(page), default_timeout=20)
    app.session_state["vocab-model"] = ("claude", "claude-haiku-4-5-20251001")
    app.session_state["vocab-custom-model"] = "claude-haiku-4-5-20251001"
    app.run()
    assert not app.exception
    assert app.title[0].value == "단어 연습문제 생성"
    assert app.selectbox(key="vocab-model-default-gpt6").value == ("bizrouter:openai/gpt-6-luna", "openai/gpt-6-luna")
    app.text_input(key="vocab-question-query").set_value("없는 단어").run()
    assert not app.exception and app.button(key="vocab-save").disabled is False
    app.text_input(key="vocab-question-query").set_value("미루").run()
    assert not app.exception and app.selectbox(key="vocab-question-word").value == 1
    assert app.button(key="vocab-save").disabled is False
    task = runner.store.tasks(batch_id)[0]
    app.radio(key=f"vocab-mode-{task['generation_id']}").set_value("주관식").run()
    assert not app.exception
    assert any("허용 답안: 미뤘어요, 미루었어요" in item.value for item in app.markdown)
    app.button(key="vocab-save").click().run()
    assert not app.exception
    assert len(bank.snapshot(1)["existing"]) == 7
    assert any("PostgreSQL 저장 확인 완료: 1단어 / 7문항" in item.value for item in app.success)


def test_prompt_editor_preview_actual_request_and_explicit_postgres_save(bank, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from topik_question_lab.step_02_planning.vocabulary_prompts import SYSTEM
    from topik_question_lab.ui import vocabulary_questions_ui as ui
    calls = []
    def invoke(**kwargs):
        calls.append(kwargs)
        return result(review(bundle()) if kwargs["operation"] == "vocabulary_review" else bundle(), kwargs["operation"])
    runner = VocabularyRunner(tmp_path, invoke)
    monkeypatch.setenv("DATABASE_URL", bank.url)
    monkeypatch.setattr(ui, "get_vocabulary_runner", lambda _: runner)
    monkeypatch.setattr(ui, "can_gateway_call", lambda _: True)
    page = Path(__file__).resolve().parents[1] / "topik_question_lab/ui/vocabulary_questions_app.py"
    app = AppTest.from_file(str(page), default_timeout=20).run()
    prefix = "vocab-prompt-" + bank.identity()
    custom = {"system": "사용자가 고친 공통 지침", "generation_prompt": "회사 생활을 소재로 출제하세요.",
              "review_prompt": "오답의 타당성을 검수하세요.", "repair_prompt": "오류 문항을 정확하게 고치세요."}
    for field, value in custom.items():
        app.text_area(key=f"{prefix}-{field}").set_value(value)
    app.run()
    assert not app.exception
    preview_system = next(area.value for area in app.text_area if area.label == "최종 System 프롬프트")
    preview_user = next(area.value for area in app.text_area if area.label == "최종 User 프롬프트")
    assert preview_system == custom["system"] and preview_user.startswith(custom["generation_prompt"])
    app.button(key="vocab-start").click().run()
    finish(runner)
    app.run()
    assert not app.exception
    batch = runner.store.batches(bank.identity())[0]
    assert all(batch["config"][key] == value for key, value in custom.items())
    assert calls[0]["system_prompt"] == preview_system and calls[0]["user_prompt"] == preview_user
    assert next(area.value for area in app.text_area if area.label == "전달한 System 프롬프트") == preview_system
    assert bank.snapshot(1)["existing"] == []
    assert app.button(key="vocab-save").label == "검수 완료 문제를 PostgreSQL에 저장"
    app.button(key="vocab-save").click().run()
    assert not app.exception and len(bank.snapshot(1)["existing"]) == 7
    assert app.button(key="vocab-save").disabled
    assert any("PostgreSQL 저장 확인 완료: 1단어 / 7문항" in notice.value for notice in app.success)
    app.button(key=f"{prefix}-reset").click().run()
    assert app.text_area(key=f"{prefix}-system").value == SYSTEM
    assert runner.store.batch(batch["batch_id"])["config"]["system"] == custom["system"]
    app.text_area(key=f"{prefix}-system").set_value("").run()
    assert not app.exception and app.button(key="vocab-start").disabled
    assert any("공통 지침" in notice.value for notice in app.error)
