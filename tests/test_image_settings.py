import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from test_bank_media_generation import snapshot, plan, image_target
from test_media_generation import FakeCloud, FakeGoogle, assert_image_color
from topik_question_lab.common.generation.bulk_generation import BulkStore
from topik_question_lab.common.media import media_provider as providers
from topik_question_lab.common.media.media_generation import MediaPipeline
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.ui.navigation import menu_page
from topik_question_lab.menus.bank_editor.bank_media_generation import generate_replacements


def test_default_image_settings_do_not_create_database(tmp_path):
    current = providers.MediaSettings.from_env()
    assert (current.image_provider, current.image_model) == ("bizrouter", providers.DEFAULT_BIZROUTER_IMAGE_MODEL)
    assert not (tmp_path / "data/bulk_generation.db").exists()


def test_settings_menu_saves_and_reopens_both_services(tmp_path):
    page = str(menu_page("settings_app.py"))
    app = AppTest.from_file(page).run()
    assert not app.exception
    assert app.radio(key="image-provider").value == "bizrouter"
    assert not (tmp_path / "data/bulk_generation.db").exists()

    for provider, model in [("google", providers.DEFAULT_GOOGLE_IMAGE_MODEL),
                            ("bizrouter", providers.DEFAULT_BIZROUTER_IMAGE_MODEL)]:
        previous = providers.MediaSettings.from_env().image_provider
        app.radio(key="image-provider").set_value(provider).run()
        assert providers.MediaSettings.from_env().image_provider == previous
        next(button for button in app.button if button.label == "저장").click().run()
        assert not app.exception and app.success
        assert BulkStore(tmp_path).settings()["image_provider"] == provider
        assert providers.MediaSettings.from_env().image_model == model
        app = AppTest.from_file(page).run()
        assert not app.exception
        assert app.radio(key="image-provider").value == provider
        assert any(model in caption.value for caption in app.caption)


def test_settings_navigation_does_not_start_batch_runner(tmp_path, monkeypatch):
    BulkStore(tmp_path).set_setting("image_provider", "google")
    monkeypatch.setattr("topik_question_lab.common.generation.runner_service.get_runner",
                        lambda *args: pytest.fail("settings must not start or recover a batch"))
    app = AppTest.from_file(str(PACKAGE_ROOT / "main.py"), default_timeout=30)
    app.switch_page(menu_page("settings_app.py").relative_to(PACKAGE_ROOT).as_posix()).run()
    assert not app.exception
    assert app.radio(key="image-provider").value == "google"


@pytest.mark.parametrize("provider", ["google", "bizrouter"])
def test_saved_service_uses_own_model_and_explicit_overrides(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("GOOGLE_IMAGE_MODEL", "google-custom-image")
    monkeypatch.setenv("BIZROUTER_IMAGE_MODEL", "bizrouter-custom-image")
    BulkStore(tmp_path).set_setting("image_provider", provider)
    current = providers.MediaSettings.from_env()
    assert (current.image_provider, current.image_model) == (provider, f"{provider}-custom-image")
    store = BulkStore(tmp_path)
    store.set_setting("image_model_google", "google-saved-image")
    store.set_setting("image_model_bizrouter", "bizrouter-saved-image")
    current = providers.MediaSettings.from_env()
    assert (current.image_provider, current.image_model) == (provider, f"{provider}-saved-image")
    other = "bizrouter" if provider == "google" else "google"
    explicit = providers.MediaSettings.from_env(image_provider=other)
    assert (explicit.image_provider, explicit.image_model) == (other, f"{other}-saved-image")
    assert providers.MediaSettings.from_env(image_model="explicit-image").image_model == "explicit-image"


@pytest.mark.parametrize("original", ["google", "bizrouter", "legacy"])
def test_saved_pipeline_keeps_service_and_model_after_preference_change(tmp_path, original):
    provider = "google" if original == "legacy" else original
    saved = providers.MediaSettings.from_env(image_provider=provider, image_model="saved-image-model").snapshot()
    if original == "legacy":
        saved.pop("image_provider")
        saved.pop("image_base_url")
    store = BulkStore(tmp_path)
    store.set_setting(f"image_model_{provider}", "changed-image-model")
    other = "bizrouter" if provider == "google" else "google"
    store.set_setting("image_provider", other)
    assert providers.MediaSettings.from_env().image_provider == other
    pipeline = MediaPipeline(tmp_path, store, "saved-batch", {"settings": saved, "target": "test"},
                             client=FakeGoogle(), cloud=FakeCloud(), registration=SimpleNamespace(target="test"))
    assert (pipeline.settings.image_provider, pipeline.settings.image_model) == (provider, "saved-image-model")


@pytest.mark.parametrize("exam_format", ["pbt", "ibt"])
def test_bank_replacement_uses_saved_google_choice_without_bizrouter_key(snapshot, tmp_path, monkeypatch, exam_format):
    BulkStore(tmp_path).set_setting("image_provider", "google")
    BulkStore(tmp_path).set_setting("image_model_google", "google-custom-image")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "synthetic-project")
    monkeypatch.setenv("GOOGLE_IMAGE_MODEL", "old-env-image")
    google = FakeGoogle()
    authenticated = []
    google.authenticate = lambda: authenticated.append(True)
    configs = []

    def client(settings):
        configs.append(settings)
        return google

    monkeypatch.setattr(providers, "GoogleMediaClient", client)
    monkeypatch.setattr(providers, "BizRouterImageClient", lambda s: pytest.fail("unexpected BizRouter request"))
    prepared = plan(snapshot(exam_format=exam_format), [image_target()])
    result = generate_replacements(prepared)
    generated = result["replacements"][0]
    assert generated["generation"]["provider"] == "google"
    assert generated["generation"]["model_name"] == "google-custom-image"
    assert len(google.images) == 1 and authenticated == [True]
    assert configs[0].image_model == "google-custom-image"
    assert_image_color(generated["data"], color=exam_format == "ibt")


def test_model_selection_sync_save_and_switch_preserve_both_choices(tmp_path, monkeypatch):
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "synthetic-project")
    closed = []
    monkeypatch.setattr(providers, "BizRouterImageClient", lambda settings: SimpleNamespace(
        list_models=lambda: ["vendor/image-a", "vendor/image-b"], close=lambda: closed.append(True)))
    monkeypatch.setattr(providers, "GoogleMediaClient", lambda settings: SimpleNamespace(
        list_models=lambda: ["gemini-99-flash-image"], close=lambda: closed.append(True)))
    page = str(menu_page("settings_app.py"))
    app = AppTest.from_file(page).run()
    next(button for button in app.button if button.label == "BizRouter 이미지 모델 동기화").click().run()
    assert not app.exception and closed == [True]
    assert "vendor/image-b" in app.selectbox(key="image-model-bizrouter").options
    assert "image_model_bizrouter" not in BulkStore(tmp_path).settings()
    next(button for button in app.button if button.label == "Google 이미지 모델 동기화").click().run()
    assert not app.exception and closed == [True, True]
    assert "gemini-99-flash-image" in app.selectbox(key="image-model-google").options
    assert not any("gemini-3-pro-image" in option for option in app.selectbox(key="image-model-google").options)
    assert "image_model_google" not in BulkStore(tmp_path).settings()
    assert providers.MediaSettings.from_env().image_provider == "bizrouter"
    app.selectbox(key="image-model-bizrouter").set_value("vendor/image-b")
    app.selectbox(key="image-model-google").set_value("gemini-99-flash-image")
    next(button for button in app.button if button.label == "저장").click().run()
    assert not app.exception
    for provider, model in [("google", "gemini-99-flash-image"), ("bizrouter", "vendor/image-b")]:
        app = AppTest.from_file(page).run()
        assert app.selectbox(key="image-model-bizrouter").value == "vendor/image-b"
        assert app.selectbox(key="image-model-google").value == "gemini-99-flash-image"
        app.radio(key="image-provider").set_value(provider)
        next(button for button in app.button if button.label == "저장").click().run()
        assert not app.exception
        assert (providers.MediaSettings.from_env().image_provider, providers.MediaSettings.from_env().image_model) == (provider, model)
    assert "synthetic-key" not in json.dumps(BulkStore(tmp_path).settings())


@pytest.mark.parametrize("result", [[], "error", ["vendor/new-image"]])
@pytest.mark.parametrize("service", ["bizrouter", "google"])
def test_sync_keeps_saved_models_even_when_catalog_changes(tmp_path, monkeypatch, result, service):
    store = BulkStore(tmp_path)
    store.set_setting("image_model_bizrouter", "vendor/saved-image")
    store.set_setting("image_model_google", "google-existing-custom")
    store.set_setting("bizrouter_image_models", '["vendor/saved-image"]')
    store.set_setting("google_image_models", '["google-existing-custom"]')
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "synthetic-project")
    closed = []

    def list_models():
        if result == "error":
            raise providers.MediaError("모델 목록 조회 실패")
        return result

    monkeypatch.setattr(providers, "BizRouterImageClient" if service == "bizrouter" else "GoogleMediaClient", lambda settings: SimpleNamespace(
        list_models=list_models, close=lambda: closed.append(True)))
    app = AppTest.from_file(str(menu_page("settings_app.py"))).run()
    label = "BizRouter" if service == "bizrouter" else "Google"
    next(button for button in app.button if button.label == f"{label} 이미지 모델 동기화").click().run()
    assert not app.exception and closed == [True]
    assert app.selectbox(key="image-model-bizrouter").value == "vendor/saved-image"
    assert app.selectbox(key="image-model-google").value == "google-existing-custom"
    assert store.settings()["image_model_bizrouter"] == "vendor/saved-image"
    assert store.settings()["image_model_google"] == "google-existing-custom"
    if not result or result == "error":
        assert json.loads(store.settings()["bizrouter_image_models"]) == ["vendor/saved-image"]
        assert json.loads(store.settings()["google_image_models"]) == ["google-existing-custom"]
        assert app.error if result == "error" else app.warning


@pytest.mark.parametrize("model", list(providers.GOOGLE_IMAGE_MODELS))
@pytest.mark.parametrize("banner", [False, True])
def test_saved_google_model_reaches_generate_content(tmp_path, monkeypatch, model, banner):
    store = BulkStore(tmp_path)
    store.set_setting("image_provider", "google")
    store.set_setting("image_model_google", model)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT_ID", "synthetic-project")
    requests = []
    data = FakeGoogle().image("test")

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"inlineData": {"data": base64.b64encode(data).decode()}}]}}]})

    client = providers.GoogleMediaClient(providers.MediaSettings.from_env(),
        credentials=SimpleNamespace(valid=True, token="synthetic"),
        client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        assert client.image("test", banner=banner) == data
    finally:
        client.close()
    assert requests[0].url.path.endswith(f"/models/{model}:generateContent")
    assert json.loads(requests[0].content)["generationConfig"]["imageConfig"]["aspectRatio"] == ("4:1" if banner else "4:3")
