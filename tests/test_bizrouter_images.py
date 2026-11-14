import base64
from dataclasses import replace
import io
import json
from types import SimpleNamespace

import httpx
from openai import OpenAI
from PIL import Image
import pytest

from test_bank_media_generation import snapshot, plan
from test_ibt_pipeline import generated_batch, COMBO
from test_media_generation import Clock, FakeGoogle, pipeline, settings, assert_image_color
from topik_question_lab.common.media import media_provider as providers, media_generation as media
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog
from topik_question_lab.common.question_bank.storage_overview import preparation_progress
from topik_question_lab.menus.bank_editor.bank_media_generation import generate_replacements


MODEL = providers.DEFAULT_BIZROUTER_IMAGE_MODEL


def image_response(content=None, finish="stop"):
    if content is None:
        data = base64.b64encode(FakeGoogle().image("sample")).decode()
        content = [{"type": "text", "text": "image"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}}]
    return {"id": "synthetic", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


def mock_images(monkeypatch, respond):
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-image-key")
    sdk = OpenAI(api_key="synthetic-image-key", base_url="https://bizrouter.test/v1", max_retries=0,
                 http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    return providers.BizRouterImageClient(providers.MediaSettings.from_env(), client=sdk)


def test_new_settings_use_nano_banana_but_old_snapshots_keep_google(monkeypatch):
    monkeypatch.setenv("GOOGLE_IMAGE_MODEL", "legacy-env-model")
    monkeypatch.setenv("BIZROUTER_API_KEY", "never-save-this-key")
    current = providers.MediaSettings.from_env()
    assert (current.image_provider, current.image_model) == ("bizrouter", MODEL)
    assert "never-save-this-key" not in json.dumps(current.snapshot())
    old = settings(image_model="saved-google-model").snapshot()
    old.pop("image_provider")
    old.pop("image_base_url")
    assert providers.MediaSettings(**old).image_provider == "google"
    assert providers.MediaSettings(**old).image_model == "saved-google-model"
    monkeypatch.setenv("BIZROUTER_IMAGE_MODEL", "google/another-image")
    monkeypatch.setenv("BIZROUTER_BASE_URL", "https://another.test/v1/")
    saved = providers.MediaSettings.from_env().snapshot()
    monkeypatch.setenv("BIZROUTER_IMAGE_MODEL", "changed-model")
    restored = providers.MediaSettings(**saved)
    assert (restored.image_model, restored.image_base_url) == ("google/another-image", "https://another.test/v1")


def test_image_sdk_disables_automatic_retries_and_uses_own_key(monkeypatch):
    calls = []
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-key")
    monkeypatch.setenv("CHATKHU_API_KEY", "wrong-key")
    monkeypatch.setattr("openai.OpenAI", lambda **kw: calls.append(kw))
    providers.BizRouterImageClient(providers.MediaSettings.from_env())
    assert calls[0]["api_key"] == "synthetic-key" and calls[0]["max_retries"] == 0
    assert calls[0]["base_url"] == "https://api.bizrouter.ai/v1"
    assert calls[0]["timeout"].read == 180
    monkeypatch.delenv("BIZROUTER_API_KEY")
    with pytest.raises(providers.MediaError, match="BIZROUTER_API_KEY"):
        providers.BizRouterImageClient(providers.MediaSettings.from_env())
    assert len(calls) == 1


def test_catalog_and_sdk_image_requests_preserve_chart_and_scene_prompts(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"id": MODEL, "output_modalities": ["text", "image"]}]})
        return httpx.Response(200, json=image_response())
    client = mock_images(monkeypatch, respond)
    try:
        client.authenticate()
        for color in (False, True):
            data = client.image("학생이 책을 읽는 장면", color=color)
            assert Image.open(io.BytesIO(data)).format == "PNG"
        chart = media.chart_image_prompt(COMBO)
        client.image(chart, chart=True)
    finally:
        client.close()
    assert requests[0].url.path == "/v1/models"
    assert all(r.headers["Authorization"] == "Bearer synthetic-image-key" for r in requests)
    bodies = [json.loads(r.content) for r in requests[1:]]
    assert all(r.url.path == "/v1/chat/completions" for r in requests[1:])
    assert all(b["model"] == MODEL and b["aspect_ratio"] == "4:3" and b["stream"] is False for b in bodies)
    assert "Black-and-white" in bodies[0]["messages"][0]["content"]
    assert "Full-color" in bodies[1]["messages"][0]["content"]
    assert bodies[2]["messages"][0]["content"] == chart
    assert "no text, labels, numbers" not in chart


@pytest.mark.parametrize("models", [None, {}, [], [{"id": MODEL, "output_modalities": ["text"]}],
                                     [{"id": "different/image", "output_modalities": ["image"]}]])
def test_catalog_rejects_unavailable_or_non_image_model(monkeypatch, models):
    client = mock_images(monkeypatch, lambda r: httpx.Response(200, json={"models": models}))
    try:
        with pytest.raises(providers.MediaError, match="모델"):
            client.authenticate()
    finally:
        client.close()


@pytest.mark.parametrize("content,finish", [
    ("No image generated", "stop"), ([], "stop"), ([], "content_filter"),
    ([{"type": "image_url", "image_url": {"url": "data:image/png;base64,!bad"}}], "stop"),
    ([{"type": "image_url", "image_url": {"url": "https://private.test/image"}}], "stop"),
    ([{"type": "image_url", "image_url": None}], "stop"),
    (None, "length"),
])
def test_invalid_images_are_rejected_without_downloading_urls(monkeypatch, content, finish):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=image_response(content, finish))
    client = mock_images(monkeypatch, respond)
    try:
        with pytest.raises(providers.MediaError, match="올바른 이미지"):
            client.image("sample")
    finally:
        client.close()
    assert len(requests) == 1


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (403, False), (429, True), (503, True)])
def test_errors_keep_retry_after_and_never_expose_provider_body(monkeypatch, status, retryable):
    client = mock_images(monkeypatch, lambda r: httpx.Response(status, headers={"Retry-After": "42"},
                                                             json={"error": "synthetic-image-key private input"}))
    try:
        with pytest.raises(providers.MediaError) as caught:
            client.image("sample")
    finally:
        client.close()
    assert caught.value.retryable is retryable
    assert "synthetic-image-key" not in str(caught.value) and "private" not in str(caught.value)
    assert str(status) in str(caught.value)
    if retryable:
        assert caught.value.retry_after == 42


def test_transient_transport_failure_is_retried_by_request_runner(monkeypatch):
    attempts = []
    clock = Clock()
    def respond(request):
        attempts.append(clock.now())
        if len(attempts) == 1:
            raise httpx.ConnectError("synthetic-image-key", request=request)
        return httpx.Response(200, json=image_response())
    client = mock_images(monkeypatch, respond)
    runner = providers.RequestRunner(settings(retries=1), now=clock.now, wait=clock.wait)
    try:
        assert runner.call("image", lambda: client.image("sample")).startswith(b"\x89PNG")
    finally:
        client.close()
    assert len(attempts) == 2 and attempts[1] - attempts[0] >= 10


def test_image_only_validation_and_routing_do_not_need_google_or_s3(monkeypatch):
    client = mock_images(monkeypatch, lambda r: httpx.Response(200, json=image_response()))
    monkeypatch.setattr(providers, "BizRouterImageClient", lambda s: client)
    monkeypatch.setattr(providers, "GoogleMediaClient", lambda s: pytest.fail("image-only must not open Google"))
    config = providers.MediaSettings.from_env()
    config.validate("reading", images=True, delivery=False)
    routed = providers.MediaClient(config)
    try:
        assert routed.image("sample").startswith(b"\x89PNG")
    finally:
        routed.close()
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT_ID"):
        config.validate("listening", images=False, delivery=False)


def test_audio_only_and_legacy_images_still_use_google(monkeypatch):
    google = FakeGoogle()
    monkeypatch.setattr(providers, "GoogleMediaClient", lambda s: google)
    monkeypatch.setattr(providers, "BizRouterImageClient", lambda s: pytest.fail("unexpected image service"))
    current = providers.MediaClient(providers.MediaSettings.from_env())
    current.speech("여자", "안녕하세요.")
    legacy = providers.MediaClient(settings())
    legacy.image("기존 작업")
    assert google.speeches == [("여자", "안녕하세요.")]
    assert google.images == ["기존 작업"]
    current.close()
    legacy.close()


@pytest.mark.parametrize("section,exam_format,remote", [("reading", "pbt", False), ("reading", "ibt", True)])
def test_preflight_only_authenticates_needed_image_service(tmp_path, monkeypatch, section, exam_format, remote):
    calls = []
    monkeypatch.setenv("BIZROUTER_API_KEY", "test-key")
    config = providers.MediaSettings.from_env(bucket="test", base_url="https://test.cloudfront.net")
    monkeypatch.setattr(media, "MediaRegistration", lambda *a: SimpleNamespace(preflight=lambda: None, target="test"))
    monkeypatch.setattr(media, "S3MediaStore", lambda *a, **k: SimpleNamespace(
        s3=SimpleNamespace(head_bucket=lambda **k: None), close=lambda: None))
    monkeypatch.setattr(providers, "GoogleMediaClient", lambda s: pytest.fail("reading does not need Google"))
    monkeypatch.setattr(providers, "BizRouterImageClient", lambda s: SimpleNamespace(
        authenticate=lambda: calls.append("catalog"), close=lambda: None))
    result = media.preflight(tmp_path, config, section, "test", exam_format)
    assert calls == (["catalog"] if remote else [])
    assert result["settings"]["image_provider"] == "bizrouter"


def test_pipeline_cache_separates_providers_and_reuses_success_after_upload_failure(tmp_path, monkeypatch):
    runner, bid, _, _ = generated_batch(tmp_path, "reading", monkeypatch)
    run = pipeline(tmp_path, bid)
    task = next(t for t in media.media_plan(run.registration.package(bid), run.settings) if t.get("model_chart"))
    run._image(task)
    run.settings = replace(run.settings, image_provider="bizrouter")
    config = {**run.config, "settings": run.settings.snapshot()}
    run.store.configure_media(bid, config)
    batch = run.store.batches()[0]
    records = collect_candidate_catalog(tmp_path).records
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["images_ready"] == 0
    run.cloud.fail = True
    with pytest.raises(providers.MediaError):
        run._image(task)
    assert len(run.client.images) == 2
    run.cloud.fail = False
    run.store.reset_media_attempts(bid)
    run._image(task)
    assert len(run.client.images) == 2
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["images_ready"] == 1
    image_tasks = [json.loads(t["snapshot_json"]) for t in run.store.media_tasks(bid) if t["kind"] == "image"]
    assert {s.get("provider", "google") for s in image_tasks} == {"google", "bizrouter"}
    run.client.close()
    run.cloud.close()


@pytest.mark.parametrize("exam_format,section,slot,role", [
    ("pbt", "listening", 1, "choice"), ("ibt", "listening", 1, "choice"),
    ("ibt", "listening", 2, "choice"), ("ibt", "reading", 7, "material"),
    ("ibt", "reading", 4, "material"), ("ibt", "reading", 5, "material"), ("ibt", "reading", 6, "material"),
])
def test_bank_replacement_uses_bizrouter_without_google_or_storage(snapshot, monkeypatch, exam_format, section, slot, role):
    requests = []
    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"id": MODEL, "output_modalities": ["image"]}]})
        return httpx.Response(200, json=image_response())
    client = mock_images(monkeypatch, respond)
    monkeypatch.setattr(providers, "BizRouterImageClient", lambda s: client)
    monkeypatch.setattr(providers, "GoogleMediaClient", lambda s: pytest.fail("unexpected Google authentication"))
    current = snapshot(section, slot, exam_format)
    prepared = plan(current, [{"kind": "image", "visual_role": role, "option_number": 1, "generate": True}])
    result = generate_replacements(prepared)
    generated = result["replacements"][0]
    assert generated["generation"]["provider"] == "bizrouter"
    assert generated["generation"]["model_name"] == MODEL
    assert_image_color(generated["data"], color=exam_format == "ibt")
    assert [r.method for r in requests] == ["GET", "POST"]
    body = json.loads(requests[-1].content)
    assert body["aspect_ratio"] == ("4:1" if section == "reading" and slot in (4, 5, 6) else "4:3")
    if section == "reading" and slot in (4, 5, 6):
        sent = json.loads(requests[-1].content)["messages"][0]["content"]
        assert sent == media.banner_image_prompt(current["item"]["content_json"]["visual_material"]["text"])
