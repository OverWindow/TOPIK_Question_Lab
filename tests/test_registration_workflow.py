from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from test_direct_publication import seed_batch, change_question
from test_storage_overview import files
from topik_question_lab.step_06_publication.registration_workflow import RegistrationWorkflow
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog
from topik_question_lab.step_06_publication.direct_publication import PublicationJobs, DirectPublicationService

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "topik_question_lab/ui/postgres_publish_app.py"
META = {"level": 4, "difficulty": 0.0, "items": {}}


@pytest.fixture
def setup_bank(postgres_database_factory, tmp_path, monkeypatch):
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    bid = seed_batch(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    return RegistrationWorkflow(tmp_path, url), bid


def keys(workflow, count=None):
    return [r.candidate.source_key for r in collect_candidate_catalog(workflow.root).records][:count]


def make_legacy(root):
    for path in (root / "data/types").glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("UPDATE runs SET batch_id=NULL")


def test_entry_and_filter_dont_create_files_or_connect_to_media(tmp_path, monkeypatch):
    import topik_question_lab.ui.runner_service as runner
    import topik_question_lab.step_06_publication.media_cloud as media
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected mutation/network")
    monkeypatch.setattr(runner, "get_runner", forbidden)
    monkeypatch.setattr(media, "preview_s3_media", forbidden)
    monkeypatch.setattr(PostgresQuestionBank, "ensure_schema", forbidden)
    before = files(tmp_path)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception
    assert app.title[0].value == "문제 등록·세트 만들기"
    assert next(m for m in app.metric if m.label == "바로 등록 가능").value == "확인 불가"
    next(b for b in app.button if b.label == "문제 세트 만들기").click().run()
    assert not app.exception and files(tmp_path) == before


def test_prepare_is_readonly_and_selected_only_add_is_atomic(setup_bank):
    w, bid = setup_bank
    before = files(w.root)
    plan = w.prepare("add", keys(w, 2), META)
    assert files(w.root) == before
    result = w.execute(plan)
    assert result["status"] == "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 2
    assert w.execute(plan)["status"] == "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 2


def test_db_changed_after_confirm_rolls_back_whole_selection(setup_bank):
    w, _ = setup_bank
    selected = keys(w, 2)
    plan = w.prepare("add", selected, META)
    w.execute(w.prepare("add", selected[:1], META))
    result = w.execute(plan)
    assert result["status"] == "conflict"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 1


def test_local_edit_or_approval_change_stops_registration(setup_bank):
    w, _ = setup_bank
    selected = ["reading:grammar_blank:1"]
    plan = w.prepare("add", selected, META)
    change_question(w.root, edit=lambda q: {**q, "explanation": "확인 후 수정"})
    assert w.execute(plan)["status"] == "stale"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 0
    with sqlite3.connect(w.root / "data/types/grammar_blank.db") as c:
        c.execute("UPDATE reviews SET data_json=?", (json.dumps({"approved": False}),))
    with pytest.raises(ValueError, match="승인"):
        w.prepare("add", selected, META)


def test_explicit_update_preserves_review_and_detects_other_writer(setup_bank):
    w, _ = setup_bank
    selected = ["reading:grammar_blank:1"]
    w.execute(w.prepare("add", selected, META))
    with w.reader.connect() as c:
        c.execute("UPDATE topik_bank.items SET review_status='active'")
    change_question(w.root, edit=lambda q: {**q, "explanation": "선택한 변경"})
    plan = w.prepare("update", selected, META)
    assert w.execute(plan)["status"] == "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT explanation,review_status FROM topik_bank.items").fetchone() == ("선택한 변경", "active")
    change_question(w.root, edit=lambda q: {**q, "explanation": "두 번째 변경"})
    plan = w.prepare("update", selected, META)
    with w.reader.connect() as c:
        c.execute("UPDATE topik_bank.items SET explanation='다른 작업의 변경'")
    assert w.execute(plan)["status"] == "conflict"


def test_lost_acknowledgement_reconciles_without_duplicate(setup_bank, monkeypatch):
    w, _ = setup_bank
    original = PostgresQuestionBank.publish_items
    def lose_response(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise ConnectionError("synthetic acknowledgement loss")
    monkeypatch.setattr(PostgresQuestionBank, "publish_items", lose_response)
    result = w.execute(w.prepare("add", keys(w, 2), META))
    assert result["status"] == "succeeded"
    assert w.retry(result["id"])["status"] == "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 2


def test_legacy_set_no_presync_and_retry_no_second_set(setup_bank):
    w, _ = setup_bank
    make_legacy(w.root)
    with pytest.raises(ValueError):
        w.prepare("set", keys(w, 49), META)
    plan = w.prepare("set", keys(w), META)
    result = w.execute(plan)
    assert result["status"] == "succeeded"
    assert w.execute(plan)["status"] == "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM topik_bank.items WHERE set_id IS NOT NULL").fetchone()[0] == 50
    with pytest.raises(ValueError, match="세트"):
        w.prepare("set", keys(w), META)


def test_ui_add_confirmation_and_registered_summary(setup_bank):
    w, _ = setup_bank
    before = files(w.root)
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    assert not app.exception
    assert next(m for m in app.metric if m.label == "바로 등록 가능").value == "50"
    picker = next(x for x in app.multiselect if x.label == "등록할 문제")
    picker.set_value(keys(w, 2)).run()
    next(b for b in app.button if b.label == "선택 내용 확인").click().run()
    assert not app.exception and files(w.root) == before
    next(b for b in app.button if b.label == "선택한 2문제 등록하기").click().run()
    assert not app.exception
    assert any("등록 완료" in s.value for s in app.success)
    assert next(m for m in app.metric if m.label == "바로 등록 가능").value == "48"


def test_auto_registered_batch_approval_preserves_media(setup_bank):
    from test_media_generation import seed_media, pipeline
    from topik_question_lab.step_06_publication.media_registration import MediaRegistration
    w, _ = setup_bank
    # A second batch uses distinct local IDs in the same authoring stores.
    media_root = w.root / "media-test"
    media_root.mkdir()
    w = RegistrationWorkflow(media_root, w.url)
    bid = seed_media(w.root)
    reg = MediaRegistration(w.root, w.url)
    pipeline(w.root, bid, registration=reg).run()
    with w.reader.connect(True) as c:
        media_before = c.execute("SELECT to_jsonb(v) FROM topik_bank.item_visual_assets v").fetchall()
    for path in (w.root / "data/types").glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("UPDATE reviews SET data_json=?", (json.dumps({"approved": True}),))
    chosen = [r.candidate.source_key for r in collect_candidate_catalog(w.root).records if r.candidate.batch_id == bid]
    plan = w.prepare("batch", chosen, META, batch_id=bid)
    assert w.execute(plan)["status"] == "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT DISTINCT review_status FROM topik_bank.items").fetchall() == [("reviewed",)]
        assert c.execute("SELECT to_jsonb(v) FROM topik_bank.item_visual_assets v").fetchall() == media_before


def test_db_target_change_blocks_execution(setup_bank):
    w, _ = setup_bank
    plan = w.prepare("add", keys(w, 1), META)
    plan["target"] = "different-target"
    assert w.execute(plan)["status"] == "stale"


def test_approval_rechecked_inside_write_transaction(setup_bank, monkeypatch):
    w, _ = setup_bank
    selected = ["reading:grammar_blank:1"]
    plan = w.prepare("add", selected, META)
    original = PostgresQuestionBank.publish_items
    def revoke_then_write(self, *args, **kwargs):
        with sqlite3.connect(w.root / "data/types/grammar_blank.db") as c:
            c.execute("UPDATE reviews SET data_json=?", (json.dumps({"approved": False}),))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(PostgresQuestionBank, "publish_items", revoke_then_write)
    assert w.execute(plan)["status"] != "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 0


def test_failed_write_can_resume_same_receipt(setup_bank, monkeypatch):
    w, _ = setup_bank
    original = PostgresQuestionBank.publish_items
    def fail(*args, **kwargs):
        raise ConnectionError("synthetic unavailable")
    monkeypatch.setattr(PostgresQuestionBank, "publish_items", fail)
    job = w.execute(w.prepare("add", keys(w, 2), META))
    assert job["status"] == "failed"
    monkeypatch.setattr(PostgresQuestionBank, "publish_items", original)
    assert w.retry(job["id"])["status"] == "succeeded"
    assert len(PublicationJobs(w.root, readonly=True).list()) == 1


def test_validation_error_cannot_register(setup_bank):
    w, _ = setup_bank
    with sqlite3.connect(w.root / "data/types/grammar_blank.db") as c:
        c.execute("UPDATE generated_questions SET validation_json=?", (json.dumps([{"code": "synthetic_error", "message": "오류"}]),))
    with pytest.raises(ValueError, match="자동검사"):
        w.prepare("add", ["reading:grammar_blank:1"], META)


def test_other_writer_even_identical_content_requires_refresh(setup_bank):
    w, _ = setup_bank
    plan = w.prepare("add", keys(w, 2), META)
    PostgresQuestionBank(w.url).publish_items(w._draft(plan))
    assert w.execute(plan)["status"] == "conflict"


def test_changed_listening_question_invalidates_shared_track_only(setup_bank):
    import uuid
    w, _ = setup_bank
    seed_batch(w.root, section="listening")
    for path in (w.root / "data/listening/types").glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("UPDATE runs SET batch_id=NULL")
    candidates = [r.candidate for r in collect_candidate_catalog(w.root).records if r.candidate.section == "listening"]
    assert w.execute(w.prepare("set", [c.source_key for c in candidates], META))["status"] == "succeeded"
    aid = uuid.uuid4()
    with w.reader.connect() as c:
        sid = c.execute("SELECT set_id FROM topik_bank.question_sets WHERE section='listening'").fetchone()[0]
        c.execute("""INSERT INTO topik_bank.tts_audio_assets(audio_asset_id,source_hash,model_name,female_voice,male_voice,
            storage_bucket,storage_path,storage_url,byte_size,narration_version,script_snapshot)
            VALUES (%s,%s,'synthetic','a','b','synthetic','audio.mp3','https://example.invalid/audio.mp3',1,'exam_track_v4','{}')""", (aid, "a"*64))
        for position in (21, 22):
            c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
                SELECT item_id,content_hash,%s,%s FROM topik_bank.items WHERE set_id=%s AND position=%s""", (aid, "b"*64, sid, position))
    edited = next(c for c in candidates if c.slot == 21)
    with sqlite3.connect(w.root / edited.source_db) as c:
        raw = c.execute("SELECT data_json FROM generated_questions WHERE id=?", (edited.generated_id,)).fetchone()[0]
        q = json.loads(raw)
        q["dialogue_turns"][0]["text"] = "새로운 듣기 대본입니다."
        c.execute("UPDATE generated_questions SET edited_json=? WHERE id=?", (json.dumps(q), edited.generated_id))
    assert w.execute(w.prepare("update", [edited.source_key], META))["status"] == "succeeded"
    with w.reader.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.item_audio_bindings WHERE is_current").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM topik_bank.tts_audio_assets").fetchone()[0] == 1
