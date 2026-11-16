from copy import deepcopy
import hashlib

from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

from test_writing_postgres import APP, bank, local_set
from topik_question_lab.common.question_bank import writing_postgres
from topik_question_lab.common.question_bank.connection import connection
from topik_question_lab.common.writing_models import WritingPayload, WritingQuestion
from topik_question_lab.menus.writing import writing_images as images
from topik_question_lab.menus.writing.writing_pipeline import load_references
from topik_question_lab.menus.writing.writing_ui import render_question


@pytest.fixture
def question():
    return next(r.question for r in load_references() if r.source_exam == 102 and r.question.type_slot == 53)


def test_upload_preserves_old_image_path_and_generation_schema(question, writing_png, writing_cloud):
    before = question.model_dump_json()
    path = writing_png(question)
    assert path.stem == hashlib.sha256(before.encode()).hexdigest()
    question.image = images.upload_image(question)
    assert images.image_path(question) == path
    assert question.content_sha256() == path.stem
    assert WritingQuestion.model_validate_json(question.model_dump_json()) == question
    assert "image" not in WritingPayload.model_json_schema()["$defs"]["WritingQuestion"]["properties"]
    assert writing_cloud.verified == [question.image.model_dump()]
    assert writing_cloud.closed == 1
    images.upload_image(question)
    assert len(writing_cloud.objects) == 1
    assert images.image_path(question).read_bytes() == path.read_bytes()


def test_changed_content_rejects_stale_image(question, writing_png, writing_cloud):
    writing_png(question)
    question.image = images.upload_image(question)
    draft = question.model_dump()
    draft["materials"][0]["title"] += " 수정"
    with pytest.raises(ValueError, match="일치하지 않습니다"):
        WritingQuestion.model_validate(draft)
    draft.pop("image")
    assert not images.image_path(WritingQuestion.model_validate(draft)).exists()


@pytest.mark.parametrize("failure", ["missing", "invalid_png", "upload", "verify"])
def test_failed_image_prevents_database_write(local_set, writing_cloud, monkeypatch, failure):
    storage, run_id = local_set
    rows = storage.list_generated(run_id)
    question = WritingQuestion.model_validate(next(r["question"] for r in rows if r["question"]["type_slot"] == 53))
    path = images.image_path(question)
    if failure == "missing":
        path.unlink()
    elif failure == "invalid_png":
        path.write_bytes(b"broken png")
    else:
        writing_cloud.failure = failure
    monkeypatch.setattr(writing_postgres, "connection", lambda *a, **k: pytest.fail("failed upload must not open a write transaction"))
    with pytest.raises(ValueError) as exc:
        writing_postgres.WritingQuestionBank("unused").save_set("test", rows)
    assert "secret-provider-error" not in str(exc.value)


def test_regeneration_updates_image_and_failed_retry_preserves_saved_set(local_set, bank, writing_cloud):
    storage, run_id = local_set
    rows = storage.list_generated(run_id)
    bank.save_set("test", rows)
    before = bank.get_set("test")
    question = WritingQuestion.model_validate(before["questions"][2]["content_json"])
    Image.new("RGB", (120, 90), "black").save(images.image_path(question), "PNG")
    writing_cloud.failure = "verify"
    with pytest.raises(ValueError, match="CloudFront"):
        bank.save_set("test", rows)
    assert bank.get_set("test") == before
    writing_cloud.failure = None
    bank.save_set("test", rows)
    after = bank.get_set("test")
    assert after["set_id"] == before["set_id"]
    old_image = before["questions"][2]["content_json"]["image"]
    new_image = after["questions"][2]["content_json"]["image"]
    assert old_image["storage_path"] != new_image["storage_path"]
    assert old_image["question_sha256"] == new_image["question_sha256"]
    assert after["questions"][:2] == before["questions"][:2]
    assert after["questions"][3] == before["questions"][3]


def test_bank_edits_preserve_or_clear_image_with_content(local_set, bank):
    storage, run_id = local_set
    bank.save_set("test", storage.list_generated(run_id))
    original = bank.get_set("test")["questions"][2]
    bank.save_question(original, original["content_json"])
    assert bank.get_set("test")["questions"][2] == original
    draft = deepcopy(original["content_json"])
    draft["materials"][0]["title"] += " 수정"
    bank.save_question(original, draft)
    saved = bank.get_set("test")["questions"][2]["content_json"]
    assert "image" not in saved
    assert saved["materials"] == draft["materials"]


def test_attach_image_to_existing_question_preserves_all_question_content(local_set, bank):
    storage, run_id = local_set
    bank.save_set("legacy", storage.list_generated(run_id))
    with connection(bank.url, readonly=False) as c:
        c.execute("UPDATE topik_bank.writing_questions SET content_json=content_json-'image' WHERE type_slot=53")
    before = bank.get_set("legacy")
    original = before["questions"][2]
    question = WritingQuestion.model_validate(original["content_json"])
    question.image = images.upload_image(question)
    bank.save_question(original, question.model_dump())
    after = bank.get_set("legacy")
    assert after["questions"][2]["content_json"].pop("image") == question.image.model_dump()
    assert after["questions"] == before["questions"]


@pytest.mark.parametrize("local_exists", [True, False])
def test_preview_uses_published_image(question, writing_png, writing_cloud, monkeypatch, local_exists):
    path = writing_png(question)
    question.image = images.upload_image(question)
    if local_exists:
        Image.new("RGB", (120, 90), "black").save(path, "PNG")
    else:
        path.unlink()
    rendered = []
    monkeypatch.setattr("topik_question_lab.menus.writing.writing_ui.st.image", lambda data, **kwargs: rendered.append(data))
    render_question(question)
    assert rendered == [question.image.storage_url]


def test_publication_ui_reports_upload_failure(local_set, bank, writing_cloud, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", bank.url)
    writing_cloud.failure = "verify"
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("6. Hub 세트 저장").run()
    app.button(key="writing-publish").click().run()
    assert not app.exception and not app.success
    assert any("CloudFront" in error.value for error in app.error)
    assert not bank.list_sets()
