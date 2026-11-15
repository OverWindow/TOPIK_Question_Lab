import io
import json

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.generation.providers import manual_result
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.writing_models import WritingQuestion
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing import writing_images as images, writing_pipeline as pipeline
from topik_question_lab.menus.writing.writing_prompts import (
    DEFAULT_IMAGE_STYLE, build_image_prompt, prompt_settings, save_prompt_settings,
)


APP = str(PACKAGE_ROOT / "menus/writing/writing_app.py")


@pytest.fixture
def question():
    return WritingQuestion.model_validate({
        "type_slot": 53, "topic": "지역 도서관 이용 변화", "data_source": "가상 조사 자료",
        "materials": [
            {"title": "이용자 수", "kind": "line", "unit": "만 명", "categories": ["2020년", "2025년"],
             "series": [{"label": "성인", "values": [12.5, 24.75]}, {"label": "청소년", "values": [8, 9]}]},
            {"title": "이용 목적 순위", "kind": "table", "columns": ["순위", "목적"],
             "rows": [["1위", "독서"], ["2위", "문화 활동"]]},
        ], "material_notes": ["원인: 운영 시간 확대", "전망: 문화 프로그램 이용 증가"],
    })


@pytest.fixture
def media(monkeypatch):
    monkeypatch.setenv("BIZROUTER_API_KEY", "test-only")

    class Client:
        calls = []
        closed = 0
        failure = None
        response = None

        def __init__(self, settings):
            assert settings.image_provider == "bizrouter"

        def authenticate(self, section):
            assert section == "writing"

        def image(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            if self.failure:
                raise self.failure
            if self.response is not None:
                return self.response
            output = io.BytesIO()
            image = Image.new("RGBA", (120, 90), (255, 0, 0, 255))
            image.putpixel((0, 0), (0, 0, 0, 0))
            image.save(output, "PNG")
            return output.getvalue()

        def close(self):
            type(self).closed += 1

    monkeypatch.setattr(images, "MediaClient", Client)
    return Client


def save_question(storage, question):
    result = manual_result("test", "test-model", "generation", json.dumps({"questions": [question.model_dump()]}))
    return pipeline.save_result(storage, result, "system", "user", [53])


def test_prompt_preserves_every_material_without_answer_or_instruction(question):
    prompt = build_image_prompt(question, "제목 띠에 얇은 이중선을 사용한다.")
    payload = json.loads(prompt.split("자료 JSON:\n", 1)[1])
    assert payload == question.model_dump(include={"topic", "data_source", "materials", "material_notes"})
    assert "제목 띠에 얇은 이중선" in prompt
    assert "입력에 없는 배수" in prompt and "반올림하지 않는다" in prompt
    assert question.instruction not in prompt
    with pytest.raises(ValueError, match="53번"):
        build_image_prompt(pipeline.load_references()[0].question)


def test_png_roundtrip_and_edits_invalidate_only_the_matching_image(tmp_path, question, media):
    path = images.generate_image(question)
    assert path == images.image_path(question)
    with Image.open(path) as image:
        assert image.format == "PNG" and image.size == (120, 90)
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert len(set(image.getpixel((1, 1)))) == 1
        assert image.info["prompt"] == media.calls[0][0]
        assert image.info["provider"] == "bizrouter"
    assert media.calls[0][1] == {"chart": True}
    assert media.closed == 1
    for field, value in [("topic", "새 소재"), ("data_source", "다른 가상 자료"),
                         ("material_notes", ["원인: 신규 프로그램"]),
                         ("materials", [{"kind": "table", "title": "새 자료", "columns": ["항목"], "rows": [["값"]]}])]:
        changed = question.model_dump()
        changed[field] = value
        changed["instruction"] = ""
        assert not images.image_path(WritingQuestion.model_validate(changed)).exists()
    changed = question.model_dump()
    changed["materials"][0]["series"][0]["values"][0] = 13
    assert not images.image_path(WritingQuestion.model_validate(changed)).exists()
    assert images.image_path(WritingQuestion.model_validate_json(question.model_dump_json())).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("failure", ["request", "invalid_image"])
def test_failed_regeneration_keeps_previous_image_and_hides_service_errors(question, media, failure):
    path = images.generate_image(question)
    original = path.read_bytes()
    if failure == "request":
        media.failure = RuntimeError("secret-token-and-service-url")
    else:
        media.response = b"not an image"
    with pytest.raises(ValueError) as error:
        images.generate_image(question)
    assert str(error.value) == images.IMAGE_ERROR
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]
    assert media.closed == 2


def test_image_failure_does_not_undo_saved_question_and_retry_uses_saved_style(tmp_path, question, media):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    receipt = save_question(storage, question)
    settings = prompt_settings(storage, 53)
    save_prompt_settings(storage, 53, {**settings, "image_style": "여백을 넉넉히 둔다."})
    media.failure = RuntimeError("secret-token")
    failed = images.generate_result_images(storage, receipt)
    assert not failed["error"] and failed["image_error"] == images.IMAGE_ERROR
    assert len(storage.list_generated()) == 1
    assert not images.image_path(question).exists()
    media.failure = None
    assert images.generate_result_images(storage, receipt) == receipt
    assert "여백을 넉넉히 둔다." in media.calls[-1][0]
    assert images.image_path(question).is_file()


@pytest.mark.parametrize("auto", [True, False])
def test_import_image_preview_download_edit_and_reload(tmp_path, question, media, auto):
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.selectbox(key="writing-type").set_value(53).run()
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    assert app.checkbox(key="writing-create-image").value
    app.checkbox(key="writing-create-image").set_value(auto).run()
    app.text_area(key="writing-manual-json-53").set_value(json.dumps({"questions": [question.model_dump()]})).run()
    app.button(key="writing-import").click().run()
    assert not app.exception and not app.error
    assert len(media.calls) == int(auto)
    if not auto:
        next(b for b in app.button if b.label == "자료 이미지 생성").click().run()
    assert not app.exception and len(media.calls) == 1
    assert any(b.proto.label == "자료 이미지 PNG 받기" for b in app.get("download_button"))
    assert app.get("imgs")
    app.run()
    assert len(media.calls) == 1
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.sidebar.radio(key="writing-stage").set_value("5. 수정·내보내기").run()
    assert not app.exception and app.get("imgs")
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    row = storage.list_generated()[0]
    draft = row["question"]
    draft["materials"][0]["series"][0]["values"][0] = 13.75
    storage.save_generated_edit(row["id"], draft)
    app.run()
    assert not app.exception and not app.get("imgs")
    assert not any(b.proto.label == "자료 이미지 PNG 받기" for b in app.get("download_button"))
    next(b for b in app.button if b.label == "자료 이미지 생성").click().run()
    assert not app.exception and app.get("imgs")
    assert "13.75" in media.calls[-1][0]
    assert len(media.calls) == 2


def test_generation_button_also_creates_image_and_workshop_persists_style(tmp_path, question, media, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setattr(pipeline, "call_provider", lambda *a, **kw: manual_result(
        "test", "test-model", "generation", json.dumps({"questions": [question.model_dump()]})))
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.selectbox(key="writing-type").set_value(53).run()
    app.sidebar.radio(key="writing-stage").set_value("3. 프롬프트 작업실").run()
    assert app.text_area(key="writing-prompt-image-style-53").value == DEFAULT_IMAGE_STYLE
    app.text_area(key="writing-prompt-image-style-53").set_value("작은 삽화를 오른쪽 아래에 배치한다.")
    next(b for b in app.button if b.label == "프롬프트 설정 저장").click().run()
    assert not app.exception
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    app.selectbox(key="writing-service").set_value("deepseek").run()
    app.button(key="writing-generate").click().run()
    assert not app.exception and not app.error
    assert len(media.calls) == 1 and "작은 삽화를 오른쪽 아래" in media.calls[0][0]
    assert app.get("imgs")
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    assert len(storage.list_generated()) == 1
    assert "image_style" not in prompt_settings(storage, 51)
