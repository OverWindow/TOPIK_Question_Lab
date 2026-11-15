"""Persist the writing generation default in the existing settings table."""
import json

from topik_question_lab.common.generation.providers import DEFAULT_PROVIDERS, provider_model


DEFAULT_MODEL_KEY = "writing_generation_default"


def _model_selection(provider, model):
    if not isinstance(provider, str) or not isinstance(model, str) or not model.strip():
        raise ValueError("기본 생성 모델 ID를 입력하세요.")
    model = model.strip()
    if provider.startswith("bizrouter:"):
        provider = "bizrouter:" + model
    elif provider not in DEFAULT_PROVIDERS:
        raise ValueError("지원하는 생성 서비스를 선택하세요.")
    return {"provider": provider, "model": model}


def generation_default(storage):
    try:
        saved = json.loads(storage.get_setting(DEFAULT_MODEL_KEY, "{}"))
        if isinstance(saved, dict):
            return _model_selection(saved.get("provider"), saved.get("model"))
    except (ValueError, TypeError):
        pass
    provider = next(iter(DEFAULT_PROVIDERS))
    return {"provider": provider, "model": provider_model(provider)}


def save_generation_default(storage, provider, model):
    selection = _model_selection(provider, model)
    storage.set_setting(DEFAULT_MODEL_KEY, json.dumps(selection, ensure_ascii=False))
