from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

from streamlit.testing.v1 import AppTest

from test_direct_publication import seed_batch, change_question
from test_media_generation import seed_media, pipeline
from topik_question_lab.step_06_publication import storage_overview as overview
from topik_question_lab.step_06_publication.media_registration import MediaRegistration
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "topik_question_lab/ui/storage_overview_app.py"


def files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_empty_overview_does_not_create_databases(tmp_path):
    before = files(tmp_path)
    result = overview.load_overview(tmp_path, "")
    assert not result["rows"] and not result["batches"] and not result["bank"]["available"]
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception
    assert next(m for m in app.metric if m.label == "PostgreSQL 문항").value == "확인 불가"
    assert files(tmp_path) == before


def test_local_counts_examples_filters_and_refresh_are_readonly(tmp_path):
    seed_batch(tmp_path, approved=False)
    with sqlite3.connect(tmp_path / "data/types/grammar_blank.db") as c:
        c.execute("CREATE TABLE examples(id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO examples VALUES (1)")
    before = files(tmp_path)
    result = overview.load_overview(tmp_path, "")
    assert sum(i["generated_questions"] for i in result["inventory"]) == 50
    assert sum(i["examples"] for i in result["inventory"]) == 1
    assert {r["status"] for r in result["rows"]} == {"PG 확인 불가"}
    assert result["batches"][0]["registered_count"] is None
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception
    next(w for w in app.multiselect if w.label == "영역").set_value(["listening"]).run()
    assert not app.exception and any("조건에 맞는 문항" in i.value for i in app.info)
    next(b for b in app.button if b.label == "현황 새로고침").click().run()
    assert not app.exception
    assert files(tmp_path) == before


def test_pending_review_comparison_and_missing_local_are_separate(tmp_path, monkeypatch):
    seed_batch(tmp_path, approved=False)
    catalog = overview.collect_candidate_catalog(tmp_path)
    items = [{"source_key": r.candidate.source_key, "item_id": str(r.candidate.item_id),
              "section": r.candidate.section, "generator_model": r.candidate.provider_key,
              "generator_version": r.candidate.model_id, "type_slot": r.candidate.slot,
              "item_type": r.candidate.question_type, "review_status": "pending_review",
              "content_json": deepcopy(r.candidate.question), "set_id": None, "created_at": r.candidate.created_at}
             for r in catalog.records]
    orphan = {**items[0], "source_key": "reading:grammar_blank:999", "item_id": "orphan"}
    items.append(orphan)
    monkeypatch.setattr(overview, "read_bank", lambda _: {"available": True, "media_available": False,
                        "items": items, "sets": [], "message": ""})
    result = overview.load_overview(tmp_path, "synthetic")
    assert sum(r["status"] == "내용 일치" for r in result["rows"]) == 50
    assert sum(r["status"] == "로컬 원본 없음" for r in result["rows"]) == 1
    assert {r["postgres_review"] for r in result["rows"]} == {"검수 대기"}
    change_question(tmp_path, edit=lambda q: {**q, "explanation": "수정한 해설"})
    items.pop(1)
    result = overview.load_overview(tmp_path, "synthetic")
    assert any(r["status"] == "내용 불일치" for r in result["rows"])
    assert any(r["status"] == "미등록" for r in result["rows"])


def test_preparation_uses_verified_current_images_and_ignores_inactive_tasks(tmp_path):
    bid = seed_media(tmp_path)
    run = pipeline(tmp_path, bid)
    run.run()
    before = files(tmp_path)
    result = overview.load_overview(tmp_path, "")
    assert result["batches"][0]["preparation"]["images_ready"] == 1
    assert result["batches"][0]["preparation"]["images_required"] == 1
    assert files(tmp_path) == before
    with sqlite3.connect(tmp_path / "data/bulk_generation.db") as c:
        c.execute("UPDATE media_tasks SET active=0 WHERE kind='verify'")
    assert overview.load_overview(tmp_path, "")["batches"][0]["preparation"]["images_ready"] == 0
    with sqlite3.connect(tmp_path / "data/bulk_generation.db") as c:
        c.execute("UPDATE media_tasks SET active=1 WHERE kind='verify'")
    with sqlite3.connect(tmp_path / "data/types/content_match_short.db") as c:
        for qid, raw in c.execute("SELECT id,edited_json FROM generated_questions WHERE edited_json IS NOT NULL").fetchall():
            q = json.loads(raw)
            q["passage"] = "교통 조사 버스: 70%, 지하철: 30%"
            q["visual_material"]["chart_spec"]["values"] = [70, 30]
            c.execute("UPDATE generated_questions SET edited_json=? WHERE id=?", (json.dumps(q), qid))
    assert overview.load_overview(tmp_path, "")["batches"][0]["preparation"]["images_ready"] == 0


def test_failed_connection_does_not_disclose_credentials(monkeypatch):
    class Driver:
        def connect(self, *args, **kwargs):
            raise RuntimeError("postgresql://test:synthetic-secret@localhost/test")
    monkeypatch.setattr(overview, "_psycopg", lambda: (Driver(), None))
    result = overview.read_bank("postgresql://test:synthetic-secret@localhost/test")
    assert not result["available"]
    assert "synthetic-secret" not in json.dumps(result)


def test_incomplete_sqlite_is_reported_without_repair(tmp_path):
    directory = tmp_path / "data/types"
    directory.mkdir(parents=True)
    (directory / "grammar_blank.db").write_bytes(b"not a sqlite database")
    before = files(tmp_path)
    result = overview.load_overview(tmp_path, "")
    assert result["errors"]
    assert files(tmp_path) == before


def test_live_bank_pending_50_questions_35_audio_and_missing_media(postgres_database_factory, tmp_path, monkeypatch):
    import psycopg
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    monkeypatch.setenv("DATABASE_URL", url)
    bid = seed_media(tmp_path, "listening")
    run = pipeline(tmp_path, bid, registration=MediaRegistration(tmp_path, url))
    run.run()
    before = files(tmp_path)

    def database_rows():
        with psycopg.connect(url) as c:
            return {t: c.execute(f"SELECT to_jsonb(r) FROM topik_bank.{t} r ORDER BY 1::text").fetchall()
                    for t in ("items", "question_sets", "tts_audio_assets", "item_visual_assets",
                              "question_set_item_audio_bindings", "schema_migrations")}

    pg_before = database_rows()
    snapshot = overview.load_overview(tmp_path, url)
    assert snapshot["bank"]["available"] and snapshot["bank"]["media_available"]
    assert len(snapshot["rows"]) == 50 and {r["status"] for r in snapshot["rows"]} == {"내용 일치"}
    assert {r["postgres_review"] for r in snapshot["rows"]} == {"검수 대기"}
    batch = snapshot["batches"][0]
    assert batch["registered_count"] == 50
    assert batch["preparation"]["audio_files"] == 35 and batch["preparation"]["audio_bound"] == 50
    assert batch["coverage"]["audio_files"] == 35 and batch["coverage"]["audio_bound"] == 50
    expected_images = batch["coverage"]["images_required"]
    assert expected_images > 0 and batch["coverage"]["images_ready"] == expected_images
    assert batch["preparation"]["images_ready"] == expected_images
    assert files(tmp_path) == before and database_rows() == pg_before

    calls = []
    monkeypatch.setattr("topik_question_lab.step_06_publication.media_cloud.preview_s3_media",
                        lambda *args: calls.append(args) or {"items": 50, "files": 35 + expected_images})
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception and not calls
    next(b for b in app.button if b.label == "선택 세트 S3·CloudFront 검증").click().run()
    assert not app.exception and len(calls) == 1
    assert any("실제 파일 검증 통과" in s.value for s in app.success)
    assert files(tmp_path) == before and database_rows() == pg_before

    with psycopg.connect(url) as c:
        c.execute("UPDATE topik_bank.question_set_item_audio_bindings SET is_current=false WHERE position=1")
        c.execute("""UPDATE topik_bank.item_visual_assets SET content_hash=%s WHERE visual_asset_id=
            (SELECT visual_asset_id FROM topik_bank.item_visual_assets LIMIT 1)""", ("0" * 64,))
    coverage = overview.load_overview(tmp_path, url)["batches"][0]["coverage"]
    assert coverage["audio_bound"] == 49 and coverage["images_ready"] == expected_images - 1


def test_empty_postgres_is_not_migrated(postgres_database_factory):
    import psycopg
    url = postgres_database_factory()
    assert not overview.read_bank(url)["available"]
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_regnamespace('topik_bank')").fetchone()[0] is None


def test_overview_navigation_does_not_start_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from topik_question_lab.step_03_generation.bulk_generation import BulkStore
    import runpy
    import streamlit as st
    BulkStore(tmp_path)
    before = files(tmp_path)
    pages = []
    monkeypatch.setattr(st, "Page", lambda path, **kwargs: pages.append((path, kwargs)) or kwargs)
    monkeypatch.setattr(st, "navigation", lambda *args, **kwargs: SimpleNamespace(title="문제 저장 현황", run=lambda: None))
    def forbidden(*args):
        raise AssertionError("Overview must not recover background jobs")
    monkeypatch.setattr("topik_question_lab.ui.runner_service.get_runner", forbidden)
    runpy.run_path(str(ROOT / "topik_question_lab/main.py"))
    assert any(p[1]["title"] == "문제 저장 현황" for p in pages)
    assert files(tmp_path) == before
