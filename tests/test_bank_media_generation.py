from copy import deepcopy
import io
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from test_media_generation import FakeGoogle, assert_image_color, seed_media, settings
from topik_question_lab.menus.bank_editor import bank_media_generation as generation
from topik_question_lab.common.question_bank.bank_editor import BankEditor, item_metadata, prepare_edit, upload_media
from topik_question_lab.common.question_bank.direct_publication import PublicationBlocked, expected_item, package_draft, _hash
from topik_question_lab.common.media.media_registration import MediaRegistration


PAGE = Path(__file__).resolve().parents[1] / "topik_question_lab/menus/bank_editor/bank_editor_app.py"


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    banks = {}

    def load(section="listening", slot=1, exam_format="pbt"):
        key = (exam_format, section)
        if key not in banks:
            root = tmp_path / f"{exam_format}-{section}"
            root.mkdir()
            if exam_format == "ibt":
                from test_ibt_pipeline import generated_batch
                _, batch, _, _ = generated_batch(root, section, monkeypatch)
            else:
                batch = seed_media(root, section)
            draft = package_draft(MediaRegistration(root, "").package(batch))
            banks[key] = [dict(expected_item(i), item_id=str(i.candidate.item_id), set_id=None,
                                   position=i.candidate.slot, source_provenance={}) for i in draft.items]
        rows = banks[key]
        return deepcopy(dict(target="test", item=next(r for r in rows if r["type_slot"] == slot), items=rows, set=None,
                             media=dict(item_visual_assets=[], item_audio_bindings=[], tts_audio_assets=[])))
    return load


def plan(snapshot, replacements, **changes):
    question = deepcopy(snapshot["item"]["content_json"])
    question.update(changes)
    return prepare_edit(snapshot, question, item_metadata(snapshot["item"]), replacements, allow_generation=True)


def image_target(n=1):
    return dict(kind="image", visual_role="choice", option_number=n, generate=True)


def generate(prepared, client=None):
    return generation.generate_replacements(prepared, settings=settings(tts_delay=0, image_delay=0, retries=0),
                                            client=client or FakeGoogle())


@pytest.mark.parametrize("exam_format", ["pbt", "ibt"])
def test_selected_image_and_manual_upload_are_prepared_without_writes(snapshot, monkeypatch, exam_format):
    current = snapshot(exam_format=exam_format)
    old = deepcopy(current)
    upload = dict(kind="image", visual_role="choice", option_number=2, data=FakeGoogle().image("upload"))
    question = deepcopy(current["item"]["content_json"])
    question["visual_options"][0]["image_prompt"] = "새로운 그림 설명"
    prepared = prepare_edit(current, question, item_metadata(current["item"]), [image_target(), upload], allow_generation=True)
    monkeypatch.setattr(BankEditor, "connect", lambda *a, **k: pytest.fail("generation must not connect to PostgreSQL"))
    monkeypatch.setattr("topik_question_lab.common.media.media_cloud.S3MediaStore", lambda *a, **k: pytest.fail("generation must not upload"))
    client = FakeGoogle()
    result = generate(prepared, client)
    assert client.images == ["새로운 그림 설명"]
    assert result["replacements"][1] == upload
    generated = result["replacements"][0]
    assert "generate" not in generated
    assert client.image_colors == [exam_format == "ibt"]
    assert_image_color(generated["data"], color=exam_format == "ibt")
    assert result["affected"] == [current["item"]["item_id"]]
    assert current == old


def test_pending_generation_cannot_be_saved(snapshot):
    current = snapshot()
    prepared = plan(current, [image_target()])
    with pytest.raises(PublicationBlocked, match="자동 생성"):
        BankEditor("").save(prepared)


@pytest.mark.parametrize("mode", ["failure", "corrupt"])
def test_generation_failure_leaves_plan_untouched_and_hides_provider_details(snapshot, mode):
    prepared = plan(snapshot(), [image_target()])
    before = deepcopy(prepared)
    client = FakeGoogle()
    def fail(*args, **kwargs):
        if mode == "corrupt":
            return b"not an image"
        raise RuntimeError("synthetic credential detail")
    client.image = fail
    with pytest.raises(PublicationBlocked) as caught:
        generate(prepared, client)
    assert "synthetic credential" not in str(caught.value)
    assert prepared == before


def test_generated_image_cannot_follow_changed_description(snapshot):
    result = generate(plan(snapshot(), [image_target()]))
    question = deepcopy(result["question"])
    question["visual_options"][0]["description"] = "다른 그림"
    with pytest.raises(PublicationBlocked, match="다시 생성"):
        prepare_edit(result["snapshot"], question, result["metadata"], result["replacements"])


def test_material_chart_uses_local_renderer_without_google(snapshot, monkeypatch):
    prepared = plan(snapshot("reading", 10), [dict(kind="image", visual_role="material", option_number=1, generate=True)])
    monkeypatch.setattr(generation, "MediaClient", lambda *a: pytest.fail("PBT chart must render locally"))
    result = generation.generate_replacements(prepared)
    assert_image_color(result["replacements"][0]["data"], color=False)


@pytest.mark.parametrize("exam_format,slot", [("pbt", 21), ("ibt", 15)])
def test_shared_audio_uses_edited_script_and_reuses_repeated_speech(snapshot, monkeypatch, exam_format, slot):
    current = snapshot(slot=slot, exam_format=exam_format)
    prepared = plan(current, [dict(kind="audio", generate=True)],
                    dialogue_turns=[dict(speaker="여자", text="수정된 대본입니다.")])
    scripts = []
    def compose(script, speech, path, config):
        scripts.append(script)
        for segment in script["segments"]:
            for turn in segment.get("turns", []):
                speech(turn["speaker"], turn["text"])
        path.write_bytes(b"test-mp3")
    monkeypatch.setattr(generation, "compose", compose)
    monkeypatch.setattr(generation.MediaSettings, "validate", lambda *a, **kw: None)
    client = FakeGoogle()
    result = generate(prepared, client)
    assert scripts[0]["positions"] == [slot, slot + 1]
    assert client.speeches == [("여자", "수정된 대본입니다.")]
    assert len(result["affected"]) == 2
    metadata = result["replacements"][0]["generation"]
    assert metadata["model_name"] == settings().tts_model
    assert metadata["input_hash"] == _hash(scripts[0])
    question = deepcopy(result["question"])
    question["dialogue_turns"][0]["text"] = "또 변경했습니다."
    with pytest.raises(PublicationBlocked, match="다시 생성"):
        prepare_edit(current, question, result["metadata"], result["replacements"])


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_audio_preview_is_a_playable_mp3(snapshot, tmp_path):
    from topik_question_lab.common.media.audio_composer import validate_mp3
    result = generate(plan(snapshot(slot=4), [dict(kind="audio", generate=True)]))
    path = tmp_path / "preview.mp3"
    path.write_bytes(result["replacements"][0]["data"])
    assert validate_mp3(path, settings()) > 0


@pytest.mark.parametrize("section,slot,role,number", [("reading", 7, "material", 1), ("listening", 2, "choice", 3)])
def test_ibt_chart_replacement_uses_image_model(snapshot, section, slot, role, number):
    current = snapshot(section, slot, "ibt")
    pending = plan(current, [dict(kind="image", visual_role=role, option_number=number, generate=True)])
    client = FakeGoogle()
    ready = generate(pending, client)
    assert len(client.images) == len(client.chart_prompts) == 1
    assert ready["replacements"][0]["option_number"] == number
    assert_image_color(ready["replacements"][0]["data"], color=True)


def test_generated_audio_upload_preserves_metadata_and_validates_bytes(monkeypatch):
    checked = []
    class Store:
        bucket, base_url = "test", "https://example.test"
        def __init__(self, *a):
            pass
        def upload(self, path, key, size, digest, mime):
            assert path.read_bytes() == b"test-mp3"
            assert key == f"media/replacements/{digest}.mp3"
            checked.append("upload")
        def verify(self, row, **kwargs):
            assert row["model_name"] == "test-model"
            checked.append("verify")
        def close(self):
            pass
    monkeypatch.setattr("topik_question_lab.common.media.media_cloud.S3MediaStore", Store)
    monkeypatch.setattr("topik_question_lab.common.media.audio_composer.validate_mp3", lambda *a: 1200)
    metadata = dict(provider="google_cloud_tts", model_name="test-model", female_voice="female", male_voice="male",
                    tts_style=dict(speakingRate=1.0, stylePrompt=""), input_hash="test")
    result = upload_media(dict(kind="audio", data=b"test-mp3", generation=metadata))
    assert result["provider"] == "google_cloud_tts" and result["duration_ms"] == 1200
    assert "input_hash" not in result and checked == ["upload", "verify"]
    assert result["storage_bucket"] == "test"
    assert result["storage_path"] == f"media/replacements/{result['file_sha256']}.mp3"
    assert result["storage_url"] == f"https://example.test/{result['storage_path']}"


@pytest.fixture
def editor_ui(snapshot, monkeypatch):
    current = snapshot()
    editor = SimpleNamespace(target="test", load=lambda _: deepcopy(current), save=Mock(return_value={"message": "저장 완료"}))
    bank = dict(available=True, media_available=True, items=[current["item"]], sets=[])
    monkeypatch.setenv("DATABASE_URL", "test")
    monkeypatch.setattr("topik_question_lab.common.question_bank.bank_editor.BankEditor", lambda _: editor)
    monkeypatch.setattr("topik_question_lab.common.question_bank.storage_overview.read_bank", lambda _: bank)
    monkeypatch.setattr("topik_question_lab.common.question_bank.direct_publication.read_batches", lambda _: [])
    return editor


def test_ui_generation_preview_does_not_repeat_on_rerun_and_requires_save(editor_ui, monkeypatch):
    editor = editor_ui
    real_generate = generation.generate_replacements
    client = FakeGoogle()
    calls = []
    def fake_generate(prepared, **kwargs):
        calls.append(prepared)
        return real_generate(prepared, settings=settings(tts_delay=0, image_delay=0, retries=0), client=client)
    monkeypatch.setattr(generation, "generate_replacements", fake_generate)
    app = AppTest.from_file(str(PAGE), default_timeout=20)
    app.session_state["bank-editor-open-item"] = editor.load("")["item"]["item_id"]
    app.run()
    assert not app.exception and not app.error
    assert len(app.get("file_uploader")) == 5
    next(r for r in app.radio if r.label == "선지 1 이미지 교체 방식").set_value("자동 생성")
    next(t for t in app.text_area if t.label == "선지 1 그림 생성 지시").set_value("수정한 장면")
    next(t for t in app.text_area if t.label == "해설").set_value("수정한 해설")
    next(b for b in app.button if b.label == "변경 내용 확인").click().run()
    assert not app.exception and not app.error
    assert client.images == ["수정한 장면"] and len(app.get("imgs")) == 1
    assert not editor.save.called
    app.run()
    assert len(calls) == 1
    next(b for b in app.button if b.label == "편집으로 돌아가기").click().run()
    assert next(t for t in app.text_area if t.label == "해설").value == "수정한 해설"
    assert next(t for t in app.text_area if t.label == "선지 1 그림 생성 지시").value == "수정한 장면"
    next(b for b in app.button if b.label == "변경 내용 확인").click().run()
    assert len(calls) == 2
    next(b for b in app.button if b.label == "PostgreSQL에 저장").click().run()
    assert not app.exception and not app.error
    editor.save.assert_called_once()
    assert editor.save.call_args.args[0]["replacements"][0]["generation"]


def test_ui_failed_generation_keeps_edits_and_never_saves(editor_ui, monkeypatch):
    monkeypatch.setattr(generation, "generate_replacements", Mock(side_effect=PublicationBlocked("생성 실패")))
    app = AppTest.from_file(str(PAGE), default_timeout=20)
    app.session_state["bank-editor-open-item"] = editor_ui.load("")["item"]["item_id"]
    app.run()
    next(r for r in app.radio if r.label == "선지 1 이미지 교체 방식").set_value("자동 생성")
    next(t for t in app.text_area if t.label == "해설").set_value("보존할 수정 내용")
    next(b for b in app.button if b.label == "변경 내용 확인").click().run()
    assert not app.exception and any(e.value == "생성 실패" for e in app.error)
    assert next(t for t in app.text_area if t.label == "해설").value == "보존할 수정 내용"
    assert not any(b.label == "PostgreSQL에 저장" for b in app.button)
    editor_ui.save.assert_not_called()


def test_ui_manual_upload_keeps_preview_and_does_not_generate(editor_ui, monkeypatch):
    import streamlit as st
    data = FakeGoogle().image("uploaded image")
    # AppTest has no file-upload driver; provide the same bytes through the widget state.
    def uploaded(label, **kwargs):
        value = io.BytesIO(data) if label == "선지 1 이미지 교체" else None
        st.session_state[kwargs["key"]] = value
        return value
    monkeypatch.setattr(st, "file_uploader", uploaded)
    generate_mock = Mock(side_effect=AssertionError("manual uploads must not generate"))
    monkeypatch.setattr(generation, "generate_replacements", generate_mock)
    app = AppTest.from_file(str(PAGE), default_timeout=20)
    app.session_state["bank-editor-open-item"] = editor_ui.load("")["item"]["item_id"]
    app.run()
    next(b for b in app.button if b.label == "변경 내용 확인").click().run()
    assert not app.exception and not app.error and len(app.get("imgs")) == 1
    generate_mock.assert_not_called()
    editor_ui.save.assert_not_called()
    next(b for b in app.button if b.label == "PostgreSQL에 저장").click().run()
    assert not app.exception and not app.error
    replacement = editor_ui.save.call_args.args[0]["replacements"][0]
    assert replacement["data"] == data and "generation" not in replacement
