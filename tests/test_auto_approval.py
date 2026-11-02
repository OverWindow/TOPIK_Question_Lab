import json
import sqlite3
import threading

import pytest

from test_bulk_generation import caller_for, prepared, wait
from test_direct_publication import seed_batch
from test_media_generation import FakeRegistration, pipeline, seed_media
from topik_question_lab.common.models import Review
from topik_question_lab.step_03_generation.bulk_generation import BulkRunner, BulkStore, storage_for
from topik_question_lab.step_03_generation.providers import call_provider
from topik_question_lab.step_04_review.legacy_approval import digest
from topik_question_lab.step_06_publication.direct_publication import PublicationBlocked
from topik_question_lab.step_06_publication.media_registration import MediaRegistration
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog


@pytest.mark.parametrize("section", ["reading", "listening"])
def test_approval_waits_for_the_last_generation_and_preserves_manual_reviews(tmp_path, monkeypatch, section):
    snapshots = prepared(tmp_path, section, monkeypatch)
    entered, finish = threading.Event(), threading.Event()
    normal = caller_for(snapshots, [])

    def generate(*args, **kwargs):
        assert args[2] == "generation"
        if args[4] == snapshots[-1]["user"]:
            entered.set()
            assert finish.wait(15)
        return normal(*args, **kwargs)

    runner = BulkRunner(tmp_path, generate)
    bid = runner.start(snapshots)
    try:
        assert entered.wait(15)
        catalog = collect_candidate_catalog(tmp_path)
        assert 0 < len(catalog.records) < 50
        assert not any(r.candidate.review.get("approved") for r in catalog.records)
        first = catalog.records[0].candidate
        storage = storage_for(tmp_path, section, first.question_type)
        storage.save_review(first.generated_id, Review(approved=False, notes="수동 미승인"))
    finally:
        finish.set()
        wait(runner)
    catalog = collect_candidate_catalog(tmp_path)
    assert len(catalog.records) == 50
    assert sum(bool(r.candidate.review.get("approved")) for r in catalog.records) == 49
    saved = next(row for row in storage.list_generated() if row["id"] == first.generated_id)
    assert saved["review"]["notes"] == "수동 미승인"
    assert runner.store.batches()[0]["status"] == "succeeded"


@pytest.mark.parametrize("section", ["reading", "listening"])
def test_legacy_interrupted_review_resumes_without_model_calls(tmp_path, monkeypatch, section):
    snapshots = prepared(tmp_path, section, monkeypatch)
    runner = BulkRunner(tmp_path, caller_for(snapshots, []))
    bid = runner.start(snapshots)
    wait(runner)
    for snapshot in snapshots:
        with storage_for(tmp_path, section, snapshot["type_id"]).connect() as c:
            c.execute("DELETE FROM reviews")
    with runner.store.connect() as c:
        c.execute("UPDATE batches SET status='interrupted',pipeline_status='needs_attention',review_config=? WHERE id=?",
                  (json.dumps({"model": "gpt-6-luna", "existing_batch": True}), bid))
    runner.provider_call = lambda *args, **kwargs: pytest.fail("A saved batch must not call a model")
    runner.start(batch_id=bid)
    wait(runner)
    assert runner.store.batches()[0]["status"] == "succeeded"
    assert len(collect_candidate_catalog(tmp_path).candidates) == 50


def test_retired_review_operations_never_open_api_client(monkeypatch):
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: pytest.fail("Review must not connect to API"))
    for operation in ("review_preflight", "review_solve", "review_quality", "review_set", "review_repair"):
        assert call_provider("gpt_6_luna", "gpt-6-luna", operation, "system", "user").error


def test_legacy_listening_batch_retries_media_without_regeneration_or_secret_logging(tmp_path, monkeypatch):
    import topik_question_lab.step_03_generation.media_generation as generation
    from test_media_generation import settings

    snapshots = prepared(tmp_path, "listening", monkeypatch)
    runner = BulkRunner(tmp_path, caller_for(snapshots, []))
    bid = runner.start(snapshots)
    wait(runner)
    for snapshot in snapshots:
        with storage_for(tmp_path, "listening", snapshot["type_id"]).connect() as c:
            c.execute("DELETE FROM reviews")
    with runner.store.connect() as c:
        c.execute("UPDATE batches SET review_config=? WHERE id=?", (json.dumps({"model": "gpt-6-luna"}), bid))
    config = {"settings": settings().snapshot(), "target": "synthetic-target"}
    monkeypatch.setattr(generation, "preflight", lambda *args: config)
    runner.provider_call = lambda *args, **kwargs: pytest.fail("Saved questions must not be regenerated or AI-reviewed")

    class Pipeline:
        fail = True

        def __init__(self, root, store, batch_id, config, **kwargs):
            if self.fail:
                raise ModuleNotFoundError("synthetic-private-provider-message")
            self.store, self.batch_id = store, batch_id

        def run(self):
            self.store.pipeline(self.batch_id, "succeeded", auto_resume=0)

    monkeypatch.setattr(generation, "MediaPipeline", Pipeline)
    runner.start(batch_id=bid, media_config=config)
    wait(runner)
    batch = runner.store.batches()[0]
    assert batch["status"] == "succeeded" and batch["pipeline_status"] == "failed"
    assert "ModuleNotFoundError" in batch["error"] and "synthetic-private" not in batch["error"]
    assert len(collect_candidate_catalog(tmp_path).candidates) == 50
    Pipeline.fail = False
    runner.start(batch_id=bid)
    wait(runner)
    assert runner.store.batches()[0]["pipeline_status"] == "succeeded"
    assert len(collect_candidate_catalog(tmp_path).records) == 50


def test_legacy_passed_approval_remains_readable_without_modifying_history(tmp_path):
    store = BulkStore(tmp_path)
    bid = seed_batch(tmp_path)
    for path in (tmp_path / "data/types").glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("DELETE FROM reviews")
    records = collect_candidate_catalog(tmp_path).records
    config = {"version": "topik-review-v1.1", "model": "gpt-6-luna"}
    summary = {"status": "passed", "config_hash": digest(config), "set_passed": True, "media_complete": True,
               "question_hashes": {r.candidate.source_key: digest(r.candidate.question) for r in records}, "files": []}
    with store.connect() as c:
        c.execute("UPDATE batches SET review_config=?,review_summary=? WHERE id=?", (json.dumps(config), json.dumps(summary), bid))
    assert len(collect_candidate_catalog(tmp_path).candidates) == 50
    with store.connect() as c:
        saved = c.execute("SELECT review_summary FROM batches WHERE id=?", (bid,)).fetchone()[0]
    assert json.loads(saved) == summary


def test_media_package_uses_current_approval_and_rechecks_changes(tmp_path):
    bid = seed_media(tmp_path)
    reg = FakeRegistration(tmp_path)
    assert reg.package(bid)["review_status"] == "pending_review"
    for path in (tmp_path / "data/types").glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("UPDATE reviews SET data_json=?", (json.dumps({"approved": True}),))
    package = reg.package(bid)
    assert package["review_status"] == "reviewed"
    with sqlite3.connect(tmp_path / "data/types/grammar_blank.db") as c:
        c.execute("UPDATE reviews SET data_json=? WHERE generated_question_id=1", (json.dumps({"approved": False}),))
    with pytest.raises(PublicationBlocked, match="변경"):
        reg._current(package)


@pytest.mark.parametrize("section", ["reading", "listening"])
def test_media_registration_saves_approved_set_atomically(postgres_database_factory, tmp_path, monkeypatch, section):
    import psycopg
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    monkeypatch.setenv("DATABASE_URL", url)
    bid = seed_media(tmp_path, section)
    for path in (tmp_path / ("data/types" if section == "reading" else "data/listening/types")).glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("UPDATE reviews SET data_json=?", (json.dumps({"approved": True}),))
    reg = MediaRegistration(tmp_path, url)
    run = pipeline(tmp_path, bid, registration=reg)
    run.run()
    pipeline(tmp_path, bid, cloud=run.cloud, registration=reg).run()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT review_status,count(*) FROM topik_bank.items GROUP BY review_status").fetchall() == [("reviewed", 50)]
        assert c.execute("SELECT review_status,count(*) FROM topik_bank.question_sets GROUP BY review_status").fetchall() == [("reviewed", 1)]
