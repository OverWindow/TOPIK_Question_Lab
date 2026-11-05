import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from test_bulk_generation import prepared, response, seed, wait
from test_ibt_pipeline import payload as ibt_response
from topik_question_lab.common.models import ProviderResult
from topik_question_lab.step_03_generation import providers
from topik_question_lab.step_03_generation.bulk_generation import (
    BulkRunner, BulkStore, custom_model, model_options, prepare_batch, profiles_for, storage_for,
)
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog


MODEL = "openai/gpt-test"
PROVIDER = "bizrouter:" + MODEL
ROOT = Path(__file__).resolve().parents[1]


def catalog(model=MODEL, **overrides):
    return {"id": model, "input_modalities": ["text", "image"], "output_modalities": ["text"],
            "supported_parameters": ["max_tokens", "response_format", "reasoning_effort", "temperature"],
            "max_output_tokens": 8192, "response_format_modes": ["json_object", "json_schema"],
            "provider_response_format_modes": {"OpenAI": ["json_object", "json_schema"]}, **overrides}


def fake_sdk(monkeypatch, payload=None, error=None):
    clients, calls = [], []

    def create(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload or {"questions": []})))],
                               usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34))

    def client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr("openai.OpenAI", client)
    return clients, calls


def test_catalog_authentication_filtering_and_safe_errors(monkeypatch):
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-bizrouter-key")
    requests = []
    entries = [catalog(), catalog("google/image", output_modalities=["text", "image"]),
               catalog("voice/tts", output_modalities=["audio"]), catalog("video/test", output_modalities=["video"]),
               catalog("embedding/test", output_modalities=["embedding"]), catalog("bizrouter/route"),
               {"id": "missing-modalities"}, None]

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"models": entries}, request=httpx.Request("GET", url))

    monkeypatch.setattr(providers.httpx, "get", get)
    models = providers.list_bizrouter_models()
    assert [item["id"] for item in models] == [MODEL]
    assert requests[0][0] == providers.BIZROUTER_BASE_URL + "/models"
    assert requests[0][1]["headers"] == {"Authorization": "Bearer synthetic-bizrouter-key"}
    assert "synthetic-bizrouter-key" not in json.dumps(models)

    def failing(*args, **kwargs):
        raise RuntimeError("synthetic-bizrouter-key")

    monkeypatch.setattr(providers.httpx, "get", failing)
    with pytest.raises(ValueError, match="모델 목록 조회에 실패") as failure:
        providers.list_bizrouter_models()
    assert "synthetic-bizrouter-key" not in str(failure.value)
    monkeypatch.delenv("BIZROUTER_API_KEY")
    with pytest.raises(ValueError, match="BIZROUTER_API_KEY"):
        providers.list_bizrouter_models()


@pytest.mark.parametrize("preset", list(providers.GENERATION_PRESETS))
def test_bizrouter_uses_own_key_model_and_supported_parameters(monkeypatch, preset):
    monkeypatch.setenv("CHATKHU_API_KEY", "chat-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("BIZROUTER_API_KEY", "bizrouter-key")
    clients, calls = fake_sdk(monkeypatch)
    result = providers.call_provider(PROVIDER, MODEL, "generation", "system", "user",
                                     generation_preset=preset, model_capabilities=catalog(), managed_retries=True)
    assert not result.error
    assert clients == [{"api_key": "bizrouter-key", "base_url": providers.BIZROUTER_BASE_URL, "max_retries": 0}]
    assert calls[0]["model"] == MODEL
    assert calls[0]["max_tokens"] == 8192
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["reasoning_effort"] == ("high" if preset == "precise_generation" else "low")
    assert "temperature" not in calls[0] and "extra_body" not in calls[0]
    assert (result.provider, result.model, result.generation_preset) == (PROVIDER, MODEL, preset)
    assert (result.input_tokens, result.output_tokens) == (12, 34)
    assert providers.provider_backend(PROVIDER) == "bizrouter"
    assert providers.provider_model(PROVIDER) == MODEL
    assert custom_model(PROVIDER) == (PROVIDER, MODEL)


def test_missing_bizrouter_key_never_uses_chatkhu(monkeypatch):
    monkeypatch.setenv("CHATKHU_API_KEY", "chat-key")
    clients, calls = fake_sdk(monkeypatch)
    assert not providers.can_gateway_call(PROVIDER)
    result = providers.call_provider(PROVIDER, MODEL, "generation", "system", "user")
    assert "BIZROUTER_API_KEY" in result.error
    assert not clients and not calls


def test_parameter_fallback_and_anthropic_json_mode():
    assert providers.generation_request_parameters(PROVIDER, MODEL, "precise_generation") == {}
    assert providers.generation_request_parameters(PROVIDER, MODEL, "precise_generation", catalog("other/model")) == {}
    claude = "anthropic/claude-test"
    params = providers.generation_request_parameters("bizrouter:" + claude, claude, "fast_draft", catalog(claude))
    assert "response_format" not in params
    mixed = catalog(provider_response_format_modes={"A": ["json_object"], "B": ["json_schema"]})
    assert "response_format" not in providers.generation_request_parameters(PROVIDER, MODEL, "fast_draft", mixed)
    limited = catalog(supported_parameters=["max_completion_tokens"], max_output_tokens=100000)
    assert providers.generation_request_parameters(PROVIDER, MODEL, "fast_draft", limited) == {"max_completion_tokens": 32768}


@pytest.mark.parametrize("images,operation", [([Path("never-read.png")], "generation"), (None, "transcription")])
def test_bizrouter_does_not_send_media(monkeypatch, images, operation):
    monkeypatch.setenv("BIZROUTER_API_KEY", "biz-key")
    clients, calls = fake_sdk(monkeypatch)
    result = providers.call_provider(PROVIDER, MODEL, operation, "system", "user", image_paths=images)
    assert "텍스트 문제 생성" in result.error
    assert not calls and not clients


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (503, True)])
def test_http_error_retry_policy(monkeypatch, status, retryable):
    from openai import APIStatusError

    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-secret")
    response = httpx.Response(status, headers={"Retry-After": "7"}, request=httpx.Request("POST", "https://example.invalid"))
    error = APIStatusError("synthetic-secret", response=response, body={"error": "synthetic-secret"})
    fake_sdk(monkeypatch, error=error)
    result = providers.call_provider(PROVIDER, MODEL, "generation", "system", "user", model_capabilities=catalog())
    assert result.error_retryable is retryable
    assert str(status) in result.error and "synthetic-secret" not in result.error
    assert result.request_parameters["max_tokens"] == 8192
    if retryable:
        assert result.error_retry_after == 7


@pytest.mark.parametrize("section", ["reading", "listening"])
@pytest.mark.parametrize("exam_format,count", [("pbt", 50), ("ibt", 30)])
def test_bulk_generation_preserves_bizrouter_snapshot_and_provenance(tmp_path, monkeypatch, section, exam_format, count):
    monkeypatch.setenv("BIZROUTER_API_KEY", "biz-key")
    seed(tmp_path, section)
    store = BulkStore(tmp_path)
    store.set_setting("bizrouter_models", json.dumps([catalog()]))
    snapshots, errors = prepare_batch(tmp_path, section, PROVIDER, MODEL, "precise_generation", exam_format)
    assert not errors and all(item["model_capabilities"]["max_output_tokens"] == 8192 for item in snapshots)
    prompts = {item["user"]: item for item in snapshots}
    calls = []
    fail = True

    def caller(provider, model, operation, system, user, generation_preset=None, model_capabilities=None):
        calls.append((provider, model, model_capabilities))
        payload = (ibt_response if exam_format == "ibt" else response)(prompts[user])
        return ProviderResult(provider=provider, model=model, operation=operation,
                              parsed_json=payload, raw_response=json.dumps(payload),
                              error="temporary failure" if fail and user == snapshots[0]["user"] else "")

    runner = BulkRunner(tmp_path, caller)
    batch_id = runner.start(snapshots)
    wait(runner)
    assert runner.store.batches()[0]["status"] == "partial"
    store.set_setting("bizrouter_models", json.dumps([catalog(max_output_tokens=1024)]))
    fail = False
    runner.start(batch_id=batch_id)
    wait(runner)
    assert runner.store.batches()[0]["status"] == "succeeded"
    assert len(calls) == len(snapshots) + 1
    assert all((p, m, c["max_output_tokens"]) == (PROVIDER, MODEL, 8192) for p, m, c in calls)
    candidates = collect_candidate_catalog(tmp_path).candidates
    assert len(candidates) == count
    assert {(item.backend, item.provider_key, item.model_id) for item in candidates} == {("bizrouter", PROVIDER, MODEL)}
    first = storage_for(tmp_path, section, snapshots[0]["type_id"])
    first.set_setting("active_providers", json.dumps([PROVIDER]))
    assert (PROVIDER, MODEL) in model_options(tmp_path)


@pytest.mark.parametrize("section,filename,button", [("reading", "app.py", "선택한 API 모델로 생성"),
                                                    ("listening", "listening_app.py", "선택 모델 병행 생성")])
def test_individual_generation_ui(tmp_path, monkeypatch, section, filename, button):
    monkeypatch.setenv("BIZROUTER_API_KEY", "biz-key")
    seed(tmp_path, section)
    type_id, profile = next(iter(profiles_for(section).items()))
    storage = storage_for(tmp_path, section, type_id)
    storage.set_setting("active_providers", json.dumps(["gpt_5_6_luna", PROVIDER]))
    storage.set_setting("bizrouter_models", json.dumps([catalog()]))
    storage.set_setting("generation_service", "bizrouter")
    storage.set_setting("generation_count", str(len(profile.question_numbers)))
    payload = response({"section": section, "type_id": type_id, "slots": profile.question_numbers})
    clients, calls = fake_sdk(monkeypatch, payload)
    app = AppTest.from_file(str(ROOT / "topik_question_lab/ui" / filename), default_timeout=30).run()
    app.sidebar.radio(key=f"{section}-work-stage").set_value("4. 문제 생성").run()
    assert not app.exception
    assert next(item for item in app.selectbox if item.label == "생성 서비스").value == "bizrouter"
    next(item for item in app.button if item.label == button).click().run()
    assert not app.exception
    assert len(calls) == 1 and calls[0]["model"] == MODEL
    assert clients[0]["api_key"] == "biz-key"
    assert calls[0]["max_tokens"] == 8192
    assert len(storage.list_generated()) == len(profile.question_numbers)
    assert {item["provider"] for item in storage.list_generated()} == {PROVIDER}


def test_bulk_ui_sync_service_selection_and_remembering(tmp_path, monkeypatch):
    monkeypatch.setenv("BIZROUTER_API_KEY", "biz-key")
    monkeypatch.setattr("topik_question_lab.ui.provider_ui.list_bizrouter_models", lambda: [catalog()])
    app = AppTest.from_file(str(ROOT / "topik_question_lab/ui/bulk_generation_app.py"), default_timeout=30).run()
    next(item for item in app.button if item.label == "BizRouter 텍스트 모델 동기화").click().run()
    next(item for item in app.selectbox if item.label == "생성 서비스").set_value("bizrouter").run()
    assert not app.exception
    assert next(item for item in app.selectbox if item.label == "모델").value == (PROVIDER, MODEL)
    assert BulkStore(tmp_path).settings()["generation_service"] == "bizrouter"
    reopened = AppTest.from_file(str(ROOT / "topik_question_lab/ui/bulk_generation_app.py"), default_timeout=30).run()
    assert not reopened.exception
    assert next(item for item in reopened.selectbox if item.label == "모델").value == (PROVIDER, MODEL)


def test_empty_service_catalog_keeps_existing_batch_controls(tmp_path, monkeypatch):
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    store = BulkStore(tmp_path)
    store.create(snapshots)
    store.set_setting("generation_service", "bizrouter")
    app = AppTest.from_file(str(ROOT / "topik_question_lab/ui/bulk_generation_app.py"), default_timeout=30).run()
    assert not app.exception
    assert any(item.label == "실패·미완료 유형 재시도" for item in app.button)
    assert not any(item.label == "읽기 50문제 생성" for item in app.button)


def test_new_bulk_plan_uses_refreshed_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("BIZROUTER_API_KEY", "biz-key")
    seed(tmp_path, "reading")
    store = BulkStore(tmp_path)
    store.set_setting("bizrouter_models", json.dumps([catalog()]))
    store.set_setting("generation_service", "bizrouter")
    app = AppTest.from_file(str(ROOT / "topik_question_lab/ui/bulk_generation_app.py"), default_timeout=30).run()
    assert not app.exception
    assert app.session_state["bulk-plan-reading"][1][0]["model_capabilities"]["max_output_tokens"] == 8192
    store.set_setting("bizrouter_models", json.dumps([catalog(max_output_tokens=4096)]))
    app.run()
    assert not app.exception
    assert app.session_state["bulk-plan-reading"][1][0]["model_capabilities"]["max_output_tokens"] == 4096
