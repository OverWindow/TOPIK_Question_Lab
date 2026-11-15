from dataclasses import replace
import io
import json
import math
import shutil
import struct
import wave

import httpx
from openai import OpenAI
import pytest
from streamlit.testing.v1 import AppTest

from test_bank_media_generation import snapshot, plan
from test_media_generation import FakeGoogle, pipeline, seed_media, settings
from topik_question_lab.common.generation.bulk_generation import BulkStore
from topik_question_lab.common.media import media_provider as providers
from topik_question_lab.common.media.audio_composer import pcm, normalize_speech, validate_mp3
from topik_question_lab.common.media.media_generation import MediaPipeline, media_plan
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog
from topik_question_lab.common.question_bank.storage_overview import preparation_progress
from topik_question_lab.common.ui.navigation import menu_page
from topik_question_lab.common.vocabulary import vocabulary_audio as vocabulary
from topik_question_lab.menus.bank_editor.bank_media_generation import generate_replacements
from topik_question_lab.menus.vocabulary import vocabulary_audio_ui


MODEL = providers.DEFAULT_BIZROUTER_TTS_MODEL
FFMPEG = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")


def tone(rate=24000, channels=1):
    frames = b"".join(struct.pack("<h", round(4000 * math.sin(2 * math.pi * 440 * i / rate))) * channels
                      for i in range(rate))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(frames)
    return buffer.getvalue(), frames


def mock_speech(monkeypatch, respond, **overrides):
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-tts-key")
    sdk = OpenAI(api_key="synthetic-tts-key", base_url="https://bizrouter.test/v1", max_retries=0,
                 http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    config = providers.MediaSettings.from_env(tts_provider="bizrouter", tts_delay=0, retries=0, **overrides)
    return providers.BizRouterSpeechClient(config, client=sdk)


@pytest.mark.parametrize("model,mime", [(MODEL, "audio/pcm"), ("google/gemini-3.8-flash-tts", "audio/wav"),
                                       ("openai/gpt-4o-mini-tts", "audio/wav")])
def test_speech_request_and_binary_contract(monkeypatch, model, mime):
    wav, frames = tone()
    bodies = []

    def respond(request):
        assert request.url.path == "/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer synthetic-tts-key"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=frames if mime == "audio/pcm" else wav, headers={"Content-Type": mime})

    client = mock_speech(monkeypatch, respond, tts_model=model)
    try:
        assert pcm(client.speech("여자", "문장을 읽습니다.", prompt="한국어로 정확히 한 번 읽으세요.")) == frames
        body = bodies[0]
        assert body["model"] == model and body["voice"] == client.settings.female
        if model.startswith("openai/"):
            assert body["input"] == "문장을 읽습니다."
            assert body["instructions"] == "한국어로 정확히 한 번 읽으세요."
            assert body["speed"] == 1 and body["response_format"] == "wav"
        else:
            assert set(body) == {"model", "input", "voice"}
            if "3.8" in model:
                assert body["input"] == "문장을 읽습니다."
            else:
                assert body["input"].endswith("\n문장을 읽습니다.")
                assert "정확히 한 번" in body["input"]
    finally:
        client.close()


@FFMPEG
def test_normalization_resamples_stereo_and_applies_gemini_speed():
    wav, _ = tone(48000, 2)
    normalized = normalize_speech(wav, "audio/wav; charset=binary", "ffmpeg")
    assert len(pcm(normalized)) == 48000
    slow = normalize_speech(tone()[1], "audio/pcm", "ffmpeg", rate=.8)
    fast = normalize_speech(wav, "audio/wav", "ffmpeg", rate=1.2)
    assert len(pcm(slow)) > 55000 and len(pcm(fast)) < 43000


@pytest.mark.parametrize("data,mime", [(b"", "audio/pcm"), (b"123", "audio/pcm"),
                                     (b'{"error":"synthetic-secret"}', "application/json"),
                                     (b"not a wav", "audio/wav")])
def test_corrupt_audio_never_reaches_composer(monkeypatch, data, mime):
    client = mock_speech(monkeypatch, lambda r: httpx.Response(200, content=data, headers={"Content-Type": mime}))
    with pytest.raises(providers.MediaError) as caught:
        client.speech("남자", "테스트")
    assert "synthetic-secret" not in str(caught.value)
    assert not caught.value.retryable
    client.close()


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (429, True), (503, True)])
def test_safe_errors_and_outer_retry_ownership(monkeypatch, status, retryable):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic-secret"}}, headers={"Retry-After": "7"})
    client = mock_speech(monkeypatch, respond)
    with pytest.raises(providers.MediaError) as caught:
        client.speech("여자", "테스트")
    assert caught.value.retryable == retryable
    assert caught.value.retry_after == 7
    assert "synthetic-secret" not in str(caught.value) and len(calls) == 1
    client.close()


def test_model_catalog_filters_chat_transcription_and_unsupported_families(monkeypatch):
    payload = {"models": [
        {"id": MODEL, "output_modalities": ["audio"]},
        {"id": MODEL, "output_modalities": ["audio"]},
        {"id": "google/gemini-3.8-flash-tts", "output_modalities": ["audio"]},
        {"id": "openai/gpt-4o-mini-tts", "output_modalities": ["audio"]},
        {"id": "openai/gpt-audio", "output_modalities": ["audio", "text"]},
        {"id": "openai/whisper-1", "output_modalities": ["text"]},
        {"id": "other/unknown-tts", "output_modalities": ["audio"]},
        {"id": "google/gemini-2.5-pro-preview-tts", "output_modalities": "audio"}, None]}
    client = mock_speech(monkeypatch, lambda r: httpx.Response(200, json=payload))
    assert client.list_models() == [MODEL, "google/gemini-3.8-flash-tts", "openai/gpt-4o-mini-tts"]
    client.authenticate()
    payload["models"] = []
    with pytest.raises(providers.MediaError, match="모델"):
        client.authenticate()
    payload["models"] = {}
    with pytest.raises(providers.MediaError, match="목록"):
        client.list_models()
    client.close()


def test_bizrouter_audio_does_not_open_google_or_images(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"id": MODEL, "output_modalities": ["audio"]}]})
        return httpx.Response(200, content=tone()[1], headers={"Content-Type": "audio/pcm"})
    speech = mock_speech(monkeypatch, respond)
    monkeypatch.setattr(providers, "GoogleMediaClient", lambda s: pytest.fail("unexpected Google authentication"))
    monkeypatch.setattr(providers, "BizRouterImageClient", lambda s: pytest.fail("audio does not require images"))
    monkeypatch.setattr(providers, "BizRouterSpeechClient", lambda s: speech)
    media = providers.MediaClient(speech.settings)
    media.authenticate("listening", images=False)
    assert pcm(media.speech("남자", "안녕하세요"))
    media.close()
    assert requests == ["/v1/models", "/v1/audio/speech"]
    assert speech.client.is_closed()


def test_validation_requires_only_selected_credentials_and_compatible_voices(monkeypatch):
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-tts-key")
    monkeypatch.setattr(providers.shutil, "which", lambda name: name)
    config = providers.MediaSettings.from_env(tts_provider="bizrouter")
    config.validate("listening", delivery=False)
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT_ID"):
        replace(config, image_provider="google").validate("listening", images=True, delivery=False)
    with pytest.raises(ValueError, match="목소리"):
        replace(config, tts_model="openai/gpt-4o-mini-tts").validate("listening", delivery=False)
    monkeypatch.setenv("BIZROUTER_API_KEY", "")
    with pytest.raises(ValueError, match="BIZROUTER_API_KEY"):
        config.validate("listening", delivery=False)


def test_settings_sync_switch_and_restore_service_models_and_voices(tmp_path, monkeypatch):
    client = mock_speech(monkeypatch, lambda r: httpx.Response(200, json={"models": [
        {"id": "openai/gpt-4o-mini-tts", "output_modalities": ["audio"]}]}))
    monkeypatch.setattr(providers, "BizRouterSpeechClient", lambda s: client)
    app = AppTest.from_file(str(menu_page("settings_app.py"))).run()
    assert not app.exception and app.radio(key="tts-provider").value == "google"
    assert not (tmp_path / "data/bulk_generation.db").exists()
    next(b for b in app.button if b.label == "BizRouter 음성 모델 동기화").click().run()
    app.selectbox(key="tts-model-bizrouter").set_value("openai/gpt-4o-mini-tts").run()
    assert app.selectbox(key="tts-female-bizrouter-openai").value == "coral"
    app.selectbox(key="tts-male-bizrouter-openai").set_value("ash")
    app.selectbox(key="tts-female-google-gemini").set_value("Kore")
    app.radio(key="tts-provider").set_value("bizrouter").run()
    assert providers.MediaSettings.from_env().tts_provider == "google"
    next(b for b in app.button if b.label == "음성 설정 저장").click().run()
    assert not app.exception
    current = providers.MediaSettings.from_env()
    assert (current.tts_provider, current.tts_model, current.female, current.male) == (
        "bizrouter", "openai/gpt-4o-mini-tts", "coral", "ash")
    reopened = AppTest.from_file(str(menu_page("settings_app.py"))).run()
    assert reopened.selectbox(key="tts-male-bizrouter-openai").value == "ash"
    reopened.radio(key="tts-provider").set_value("google").run()
    next(b for b in reopened.button if b.label == "음성 설정 저장").click().run()
    assert providers.MediaSettings.from_env().female == "Kore"
    assert providers.MediaSettings.from_env(tts_provider="bizrouter").male == "ash"


@pytest.mark.parametrize("empty", [True, False])
def test_failed_or_empty_sync_preserves_saved_preferences(tmp_path, monkeypatch, empty):
    store = BulkStore(tmp_path)
    store.set_setting("bizrouter_tts_models", json.dumps([MODEL]))
    store.set_setting("tts_model_bizrouter", MODEL)
    before = store.settings()
    client = mock_speech(monkeypatch, lambda r: httpx.Response(200 if empty else 503, json={"models": []}))
    monkeypatch.setattr(providers, "BizRouterSpeechClient", lambda s: client)
    app = AppTest.from_file(str(menu_page("settings_app.py"))).run()
    next(b for b in app.button if b.label == "BizRouter 음성 모델 동기화").click().run()
    assert not app.exception and (app.warning if empty else app.error)
    assert store.settings() == before


def test_vocabulary_ui_allows_bizrouter_without_google_project(tmp_path, monkeypatch):
    BulkStore(tmp_path).set_setting("tts_provider", "bizrouter")
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-tts-key")
    monkeypatch.setenv("S3_BUCKET", "synthetic-bucket")
    monkeypatch.setenv("CLOUDFRONT_BASE_URL", "https://media.example.test")
    monkeypatch.setattr(vocabulary_audio_ui, "search_vocabulary", lambda *a, **kw: {
        "rows": [], "total": 0, "available": True})
    app = AppTest.from_string('''
from topik_question_lab.common.paths import application_root
from topik_question_lab.common.media.media_provider import MediaSettings
from topik_question_lab.menus.vocabulary.vocabulary_audio_ui import render_audio_controls
render_audio_controls("", application_root(), {"audio_schema": True}, {"korean": "사과", "vocabulary_id": 1},
                      level=3, query="", settings=MediaSettings.from_env(), speaker="여자")
''').run()
    assert not app.exception
    assert not app.button(key="vocabulary-audio-single").disabled
    assert any("BizRouter" in caption.value for caption in app.caption)


def test_precedence_and_legacy_resume_reuse_google_cache(tmp_path, monkeypatch):
    bid = seed_media(tmp_path)
    run = pipeline(tmp_path, bid)
    original = run._speech("여자", "기존 문장")
    legacy = run.settings.snapshot()
    legacy.pop("tts_provider")
    legacy.pop("tts_base_url")
    store = run.store
    store.set_setting("tts_provider", "bizrouter")
    store.set_setting("tts_model_bizrouter", MODEL)
    monkeypatch.setenv("BIZROUTER_TTS_MODEL", "environment-model")
    assert providers.MediaSettings.from_env().tts_model == MODEL
    assert providers.MediaSettings.from_env(tts_model="explicit").tts_model == "explicit"
    resumed = MediaPipeline(tmp_path, store, bid, {"settings": legacy, "target": run.registration.target},
                            client=FakeGoogle(), cloud=run.cloud, registration=run.registration)
    assert resumed.settings.tts_provider == "google"
    assert resumed._speech("여자", "기존 문장") == original and not resumed.client.speeches
    resumed.settings = replace(resumed.settings, tts_provider="bizrouter")
    resumed._speech("여자", "기존 문장")
    assert len(resumed.client.speeches) == 1
    tasks = [json.loads(t["snapshot_json"]) for t in store.media_tasks(bid) if t["kind"] == "tts"]
    assert len(tasks) == 2 and {t.get("provider", "google") for t in tasks} == {"google", "bizrouter"}


@FFMPEG
def test_audio_progress_and_retry_reuse_only_current_provider(tmp_path):
    bid = seed_media(tmp_path, "listening")
    run = pipeline(tmp_path, bid)
    task = next(t for t in media_plan(run.registration.package(bid), run.settings) if t["kind"] == "audio")
    run._audio(task)
    run.settings = replace(run.settings, tts_provider="bizrouter")
    config = {**run.config, "settings": run.settings.snapshot()}
    run.store.configure_media(bid, config)
    batch = run.store.batches()[0]
    records = collect_candidate_catalog(tmp_path).records
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["audio_files"] == 0
    run.cloud.fail = True
    with pytest.raises(providers.MediaError):
        run._audio(task)
    calls = len(run.client.speeches)
    run.cloud.fail = False
    run.store.reset_media_attempts(bid)
    run._audio(task)
    assert len(run.client.speeches) == calls
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["audio_files"] == 1


@FFMPEG
def test_bulk_audio_and_bank_replacement_keep_bizrouter_provenance(tmp_path, monkeypatch, snapshot):
    speech = mock_speech(monkeypatch, lambda r: httpx.Response(200, content=tone()[1], headers={"Content-Type": "audio/pcm"}))
    bid = seed_media(tmp_path)
    run = pipeline(tmp_path, bid, google=speech)
    run.settings = replace(run.settings, tts_provider="bizrouter", tts_model=MODEL)
    script = {"segments": [{"kind": "speech", "speaker": "여자", "text": "테스트입니다."}]}
    row = run._audio({"script": script, "positions": [1]})
    assert row["provider"] == "bizrouter" and row["duration_ms"] > 0
    prepared = plan(snapshot(slot=4), [{"kind": "audio", "generate": True}])
    result = generate_replacements(prepared, settings=speech.settings, client=speech)
    generated = result["replacements"][0]
    assert generated["generation"]["provider"] == "bizrouter"
    path = tmp_path / "preview.mp3"
    path.write_bytes(generated["data"])
    assert validate_mp3(path, speech.settings) > 0
    speech.close()


@FFMPEG
def test_vocabulary_cache_and_status_use_selected_service(tmp_path, monkeypatch):
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=tone()[1], headers={"Content-Type": "audio/pcm"})
    client = mock_speech(monkeypatch, respond)
    profile = vocabulary.audio_profile(client.settings)
    config = vocabulary.generation_config({"korean": "사과"}, profile)
    runner = providers.RequestRunner(client.settings)
    first = vocabulary._cached_audio(tmp_path, config, None, client.settings, client, runner)
    second = vocabulary._cached_audio(tmp_path, config, None, client.settings, client, runner)
    assert first == second and first[1] > 0 and len(calls) == 1
    assert vocabulary.WORD_NARRATION_PROMPT in calls[0]["input"]
    row = {"korean": "사과", "audio_generation_config": config, "audio_source_hash": vocabulary.digest(config),
           "audio_storage_path": "synthetic.mp3"}
    assert vocabulary.audio_status(row, profile) == "ready"
    assert vocabulary.audio_status(row, vocabulary.audio_profile(settings())) == "stale"
    other = replace(client.settings, tts_base_url="https://other.test/v1")
    assert vocabulary.audio_status(row, vocabulary.audio_profile(other)) == "stale"
    modern = replace(client.settings, tts_model="google/gemini-3.8-flash-tts")
    assert vocabulary.audio_profile(modern)["style_prompt"] == ""
    assert vocabulary.audio_profile(settings())["provider"] == "google_cloud_tts"
    assert "base_url" not in vocabulary.audio_profile(settings())
    client.close()
