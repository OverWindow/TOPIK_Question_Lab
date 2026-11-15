import json

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.common.generation.bulk_generation import BulkStore
from topik_question_lab.common.generation.providers import DEFAULT_PROVIDERS, manual_result, provider_backend, provider_model
from topik_question_lab.common.paths import PACKAGE_ROOT
from topik_question_lab.common.ui.navigation import menu_page
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing import writing_pipeline
from topik_question_lab.menus.writing.writing_settings import (
    DEFAULT_MODEL_KEY, generation_default, save_generation_default,
)


SETTINGS = str(menu_page("writing_settings_app.py"))
WRITING = str(menu_page("writing_app.py"))


def catalog(*ids):
    return json.dumps([
        {"id": value, "input_modalities": ["text"], "output_modalities": ["text"],
         "supported_parameters": ["max_tokens"], "max_output_tokens": 4096}
        for value in ids
    ])


@pytest.mark.parametrize("service", ["chatkhu", "deepseek", "bizrouter"])
def test_default_save_reopen_and_analysis_generation_selection(tmp_path, service):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    storage.set_setting("bizrouter_models", catalog("vendor/model-a", "vendor/model-b"))
    app = AppTest.from_file(SETTINGS).run()
    assert not app.exception
    app.selectbox(key="writing-settings-service").set_value(service).run()
    providers = [p for p in DEFAULT_PROVIDERS if provider_backend(p) == service]
    provider = "bizrouter:vendor/model-b" if service == "bizrouter" else providers[-1]
    app.selectbox(key=f"writing-settings-provider-{service}").set_value(provider).run()
    model = "vendor/model-b" if service == "bizrouter" else f"custom-{service}-model"
    app.text_input(key=f"writing-settings-model-{provider}").set_value(model).run()
    assert storage.get_setting(DEFAULT_MODEL_KEY) == ""
    app.button(key="writing-settings-save").click().run()
    assert not app.exception and app.success
    saved = {"provider": provider, "model": model}
    assert generation_default(WritingStorage(storage.path)) == saved
    reopened = AppTest.from_file(SETTINGS).run()
    assert not reopened.exception
    assert reopened.selectbox(key="writing-settings-service").value == service
    assert reopened.selectbox(key=f"writing-settings-provider-{service}").value == provider
    assert reopened.text_input(key=f"writing-settings-model-{provider}").value == model
    writing = AppTest.from_file(WRITING, default_timeout=20).run()
    for stage, prefix in (("2. 유형 분석", "writing-analysis"), ("4. 문제 생성", "writing")):
        writing.sidebar.radio(key="writing-stage").set_value(stage).run()
        for slot in (51, 52, 53, 54):
            writing.selectbox(key="writing-type").set_value(slot).run()
            assert not writing.exception
            assert writing.selectbox(key=f"{prefix}-service").value == service
            assert writing.selectbox(key=f"{prefix}-provider-{service}").value == provider
            assert writing.text_input(key=f"{prefix}-model-{provider}").value == model


def test_bizrouter_custom_model_without_catalog_and_blank_save_blocked(tmp_path):
    app = AppTest.from_file(SETTINGS).run()
    app.selectbox(key="writing-settings-service").set_value("bizrouter").run()
    assert app.button(key="writing-settings-save").disabled
    assert app.button(key="writing-settings-bizrouter-sync").disabled
    app.text_input(key="writing-settings-custom-model-bizrouter").set_value("  vendor/custom-model  ").run()
    app.button(key="writing-settings-save").click().run()
    assert not app.exception and app.success
    saved = generation_default(WritingStorage(tmp_path / "data/writing_pbt.db"))
    assert saved == {"provider": "bizrouter:vendor/custom-model", "model": "vendor/custom-model"}
    assert app.selectbox(key="writing-settings-provider-bizrouter").value == saved["provider"]
    app.text_input(key=f"writing-settings-model-{saved['provider']}").set_value(" ").run()
    assert app.button(key="writing-settings-save").disabled
    assert generation_default(WritingStorage(tmp_path / "data/writing_pbt.db")) == saved


@pytest.mark.parametrize("sync_result", ["new_catalog", "empty", "error"])
def test_bizrouter_sync_preserves_saved_model(tmp_path, monkeypatch, sync_result):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    save_generation_default(storage, "bizrouter:vendor/saved", "vendor/saved")
    storage.set_setting("bizrouter_models", catalog("vendor/saved"))
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-key")

    def sync():
        if sync_result == "error":
            raise ValueError("모델 목록 조회 실패")
        return json.loads(catalog("vendor/new")) if sync_result == "new_catalog" else []

    monkeypatch.setattr("topik_question_lab.common.ui.provider_ui.list_bizrouter_models", sync)
    app = AppTest.from_file(SETTINGS).run()
    app.button(key="writing-settings-bizrouter-sync").click().run()
    assert not app.exception
    assert app.selectbox(key="writing-settings-provider-bizrouter").value == "bizrouter:vendor/saved"
    if sync_result == "new_catalog":
        assert "BizRouter · vendor/new" in app.selectbox(key="writing-settings-provider-bizrouter").options
    if sync_result == "error":
        assert app.error
    app = AppTest.from_file(SETTINGS).run()
    assert not app.exception
    assert app.text_input(key="writing-settings-model-bizrouter:vendor/saved").value == "vendor/saved"
    assert generation_default(storage)["model"] == "vendor/saved"
    assert "synthetic-key" not in storage.get_setting(DEFAULT_MODEL_KEY)


@pytest.mark.parametrize("operation,stage,button", [
    ("analysis", "2. 유형 분석", "writing-analyze-51"),
    ("generation", "4. 문제 생성", "writing-generate"),
])
def test_saved_bizrouter_model_and_capabilities_reach_request(tmp_path, monkeypatch, operation, stage, button):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    save_generation_default(storage, "bizrouter:vendor/default", "vendor/default")
    storage.set_setting("bizrouter_models", catalog("vendor/default"))
    monkeypatch.setenv("BIZROUTER_API_KEY", "synthetic-key")
    calls = []

    def generate(*args, **kwargs):
        calls.append((args, kwargs))
        if operation == "analysis":
            payload = {"type_slot": 51, "analysis_guide": "수신자와 요청 목적이 분명한 실용문"}
        else:
            question = next(r.question for r in writing_pipeline.load_references() if r.question.type_slot == 51)
            payload = {"questions": [question.model_dump()]}
        return manual_result(args[0], args[1], operation, json.dumps(payload))

    monkeypatch.setattr(writing_pipeline, "call_provider", generate)
    app = AppTest.from_file(WRITING, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value(stage).run()
    app.button(key=button).click().run()
    assert not app.exception and not app.error
    assert len(calls) == 1
    assert calls[0][0][:3] == ("bizrouter:vendor/default", "vendor/default", operation)
    assert calls[0][1]["model_capabilities"]["max_output_tokens"] == 4096
    run = storage.list_runs(operation)[0]
    assert (run["provider"], run["model"]) == ("bizrouter:vendor/default", "vendor/default")


def test_per_run_override_and_changed_default_in_same_session(tmp_path):
    storage = WritingStorage(tmp_path / "data/writing_pbt.db")
    save_generation_default(storage, "bizrouter:vendor/first", "vendor/first")
    app = AppTest.from_file(WRITING, default_timeout=20).run()
    app.sidebar.radio(key="writing-stage").set_value("4. 문제 생성").run()
    app.selectbox(key="writing-service").set_value("deepseek").run()
    provider = app.selectbox(key="writing-provider-deepseek").value
    app.text_input(key=f"writing-model-{provider}").set_value("temporary-deepseek").run()
    app.text_area(key="writing-topic-51").set_value("도서관 상황").run()
    assert not app.exception
    assert app.selectbox(key="writing-service").value == "deepseek"
    assert app.text_input(key=f"writing-model-{provider}").value == "temporary-deepseek"
    assert generation_default(storage)["model"] == "vendor/first"
    save_generation_default(storage, "bizrouter:vendor/second", "vendor/second")
    app.run()
    assert not app.exception
    assert app.selectbox(key="writing-service").value == "bizrouter"
    assert app.text_input(key="writing-model-bizrouter:vendor/second").value == "vendor/second"
    app.sidebar.radio(key="writing-stage").set_value("2. 유형 분석").run()
    assert app.selectbox(key="writing-analysis-service").value == "bizrouter"
    assert app.text_input(key="writing-analysis-model-bizrouter:vendor/second").value == "vendor/second"
    app.selectbox(key="writing-analysis-service").set_value("deepseek").run()
    provider = app.selectbox(key="writing-analysis-provider-deepseek").value
    app.text_input(key=f"writing-analysis-model-{provider}").set_value("temporary-analysis-model").run()
    app.selectbox(key="writing-type").set_value(52).run()
    assert app.text_input(key=f"writing-analysis-model-{provider}").value == "temporary-analysis-model"
    assert generation_default(storage)["model"] == "vendor/second"
    save_generation_default(storage, "bizrouter:vendor/third", "vendor/third")
    app.run()
    assert not app.exception
    assert app.selectbox(key="writing-analysis-service").value == "bizrouter"
    assert app.text_input(key="writing-analysis-model-bizrouter:vendor/third").value == "vendor/third"


def test_settings_page_navigation_does_not_start_runner(tmp_path, monkeypatch):
    BulkStore(tmp_path).set_setting("image_provider", "google")
    monkeypatch.setattr("topik_question_lab.common.generation.runner_service.get_runner",
                        lambda *args: pytest.fail("writing settings must not start a batch runner"))
    app = AppTest.from_file(str(PACKAGE_ROOT / "main.py"), default_timeout=30)
    app.switch_page(menu_page("writing_settings_app.py").relative_to(PACKAGE_ROOT).as_posix()).run()
    assert not app.exception
    assert any(title.value == "쓰기 설정" for title in app.title)


@pytest.mark.parametrize("raw", ['null', '[]', 'invalid json', '{"provider":"unknown","model":"x"}',
                                  '{"provider":"deepseek","model":" "}'])
def test_invalid_saved_setting_uses_existing_initial_default(tmp_path, raw):
    storage = WritingStorage(tmp_path / "writing.db")
    storage.set_setting(DEFAULT_MODEL_KEY, raw)
    provider = next(iter(DEFAULT_PROVIDERS))
    assert generation_default(storage) == {"provider": provider, "model": provider_model(provider)}
