from __future__ import annotations

import json
import base64
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from topik_question_lab.common.models import ProviderName, ProviderResult


CHATKHU_BASE_URL = os.getenv("CHATKHU_BASE_URL", "https://factchat-cloud.mindlogic.ai/v1/gateway").rstrip("/")
CHATKHU_WEB_URL = os.getenv("CHATKHU_WEB_URL", "https://chat.khu.ac.kr")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_WEB_URL = os.getenv("DEEPSEEK_WEB_URL", "https://chat.deepseek.com")
DEEPSEEK_API_KEYS_URL = "https://platform.deepseek.com/api_keys"
BIZROUTER_BASE_URL = os.getenv("BIZROUTER_BASE_URL", "https://api.bizrouter.ai/v1").rstrip("/")
BIZROUTER_WEB_URL = "https://bizrouter.ai"
BIZROUTER_PREFIX = "bizrouter:"
SERVICE_LABELS = {"chatkhu": "ChatKHU", "bizrouter": "BizRouter", "deepseek": "DeepSeek 직접 연결"}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    label: str
    model: str
    gateway_supported: bool = True
    backend: str = "chatkhu"


@dataclass(frozen=True)
class GenerationPreset:
    preset_id: str
    label: str
    description: str
    prompt_instruction: str


GENERATION_PRESETS = {
    "fast_draft": GenerationPreset(
        "fast_draft",
        "빠른 초안",
        "짧은 추론으로 빠르게 초안을 만들되 필수 형식을 지킵니다.",
        "빠르게 생성하되 요청된 문항 수, JSON 스키마, 정답의 유일성을 모두 지키십시오.",
    ),
    "diverse_draft": GenerationPreset(
        "diverse_draft",
        "다양한 초안",
        "예시와 겹치지 않는 다양한 소재의 초안을 만듭니다.",
        "기존 예시와 겹치지 않도록 소재, 상황, 인물을 다양화하고 요청된 JSON 스키마를 지키십시오.",
    ),
    "precise_generation": GenerationPreset(
        "precise_generation",
        "정밀 생성",
        "충분히 추론한 뒤 TOPIK 적합성과 문항 품질을 검토합니다.",
        "정답 유일성, 오답의 타당성, 목표 TOPIK 수준 적합성을 최종 검토한 뒤 JSON 객체만 출력하십시오.",
    ),
    "format_repair": GenerationPreset(
        "format_repair",
        "형식 오류 수정",
        "출력 형식 복구에 집중하고 임의 설명을 억제합니다.",
        "설명이나 코드 펜스를 덧붙이지 말고 요청된 구조와 필드를 정확히 갖춘 JSON 객체만 출력하십시오.",
    ),
}
DEFAULT_GENERATION_PRESET = "precise_generation"
GENERATION_OUTPUT_TOKEN_LIMIT = 32_768


# These are initial suggestions only. The models enabled for ChatKHU can differ by
# account, so the UI can fetch and display the tenant's actual model list.
DEFAULT_PROVIDERS = {
    "gpt_5_6_luna": ProviderConfig("gpt_5_6_luna", "GPT-5.6 Luna", "gpt-5.6-luna"),
    "gpt_6_luna": ProviderConfig("gpt_6_luna", "GPT-6 Luna", "gpt-6-luna"),
    "gpt_5_3_chat": ProviderConfig("gpt_5_3_chat", "GPT 5.3 Chat", "gpt-5.3-chat-latest"),
    "claude": ProviderConfig("claude", "Claude Haiku 4.5", "claude-haiku-4-5-20251001"),
    "gemini_3_5_flash": ProviderConfig(
        "gemini_3_5_flash",
        "Gemini 3.5 Flash",
        "gemini-3.5-flash",
    ),
    "gemini": ProviderConfig("gemini", "Gemini 3.1 Flash Lite", "gemini-3.1-flash-lite"),
    "k_exaone": ProviderConfig("k_exaone", "K-EXAONE", "LGAI-EXAONE/K-EXAONE-236B-A23B"),
    "solar_pro3": ProviderConfig("solar_pro3", "Solar Pro 3", "solar-pro3"),
    "llama": ProviderConfig(
        "llama",
        "Llama 4 Maverick",
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
    ),
    "gemma": ProviderConfig("gemma", "Gemma 3 27B", "google/gemma-3-27b-it"),
    "gpt_5_4_nano": ProviderConfig("gpt_5_4_nano", "GPT-5.4 Nano", "gpt-5.4-nano"),
    "deepseek": ProviderConfig(
        "deepseek",
        "DeepSeek V4 Flash",
        "deepseek-v4-flash",
        backend="deepseek",
    ),
    "deepseek_v4_1_flash": ProviderConfig(
        "deepseek_v4_1_flash",
        "DeepSeek V4.1 Flash",
        "deepseek-flash",
        backend="deepseek",
    ),
    "deepseek_v4_pro": ProviderConfig(
        "deepseek_v4_pro",
        "DeepSeek V4 Pro",
        "deepseek-v4-pro",
        backend="deepseek",
    ),
}

DEFAULT_ACTIVE_PROVIDERS = [
    "gpt_5_6_luna",
    "claude",
    "gemini_3_5_flash",
]

LEGACY_PROVIDER_LABELS = {
    "gpt_5_1": "GPT 5.1 (기존 기록)",
    "gpt_5_2": "GPT 5.2 (기존 기록)",
    "openai": "GPT (기존 기록)",
}


def has_chatkhu_api_key() -> bool:
    return bool(os.getenv("CHATKHU_API_KEY"))


def has_deepseek_api_key() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY"))


def has_bizrouter_api_key() -> bool:
    return bool(os.getenv("BIZROUTER_API_KEY"))


def provider_model(provider: str) -> str:
    if provider.startswith(BIZROUTER_PREFIX):
        return provider[len(BIZROUTER_PREFIX):]
    config = DEFAULT_PROVIDERS.get(provider)
    return config.model if config else provider


def provider_backend(provider: str, model: str = "") -> str:
    if provider.startswith(BIZROUTER_PREFIX):
        return "bizrouter"
    config = DEFAULT_PROVIDERS.get(provider)
    if config:
        return config.backend
    candidate = (model or provider).lower()
    return "deepseek" if candidate.startswith("deepseek-") else "chatkhu"


def generation_parameter_family(provider: str, model: str) -> str | None:
    """Return the generation-preset API family supported by this model."""
    if provider_backend(provider, model) == "bizrouter":
        return "bizrouter"
    if provider_backend(provider, model) == "deepseek":
        return "deepseek"
    normalized_model = model.strip().lower().rsplit("/", 1)[-1]
    if normalized_model.startswith("gpt-"):
        return "gpt"
    return None


def supports_generation_presets(provider: str, model: str) -> bool:
    return generation_parameter_family(provider, model) is not None


def parse_generation_presets(raw: str | None) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(provider): str(preset_id)
        for provider, preset_id in value.items()
        if str(preset_id) in GENERATION_PRESETS
    }


def dump_generation_presets(values: dict[str, str]) -> str:
    validated = {
        str(provider): str(preset_id)
        for provider, preset_id in values.items()
        if str(preset_id) in GENERATION_PRESETS
    }
    return json.dumps(validated, ensure_ascii=False, sort_keys=True)


def resolve_generation_preset(raw_settings: str | None, provider: str, model: str) -> str | None:
    if not supports_generation_presets(provider, model):
        return None
    return parse_generation_presets(raw_settings).get(provider, DEFAULT_GENERATION_PRESET)


def generation_preset_prompt(system_prompt: str, preset_id: str | None) -> str:
    system_prompt = re.sub(
        r"\n*\[문제 생성 프리셋\]\n- 이름: [^\n]*\n- 지침: [^\n]*\n"
        r"- 출력: 최상위 값이 JSON 객체인 유효한 JSON만 반환하십시오\.",
        "", system_prompt,
    )
    if not preset_id:
        return system_prompt
    preset = GENERATION_PRESETS.get(preset_id, GENERATION_PRESETS[DEFAULT_GENERATION_PRESET])
    suffix = (
        "\n\n[문제 생성 프리셋]\n"
        f"- 이름: {preset.label}\n"
        f"- 지침: {preset.prompt_instruction}\n"
        "- 출력: 최상위 값이 JSON 객체인 유효한 JSON만 반환하십시오."
    )
    return system_prompt.rstrip() + suffix


def generation_request_parameters(
    provider: str, model: str, preset_id: str | None, model_capabilities: dict | None = None,
) -> dict[str, object]:
    family = generation_parameter_family(provider, model)
    if family is None:
        return {}
    selected = preset_id if preset_id in GENERATION_PRESETS else DEFAULT_GENERATION_PRESET
    if family == "bizrouter":
        # Unknown models use prompt instructions only, never another gateway's defaults.
        capabilities = model_capabilities or {}
        if capabilities.get("id") != model:
            return {}
        supported = capabilities.get("supported_parameters", [])
        parameters: dict[str, object] = {}
        limit = capabilities.get("max_output_tokens")
        if isinstance(limit, int) and limit > 0:
            token_key = next((key for key in ("max_tokens", "max_completion_tokens") if key in supported), None)
            if token_key:
                parameters[token_key] = min(GENERATION_OUTPUT_TOKEN_LIMIT, limit)
        modes = capabilities.get("response_format_modes", [])
        bindings = capabilities.get("provider_response_format_modes", {})
        if ("response_format" in supported and "json_object" in modes
                and not model.startswith("anthropic/")
                and all("json_object" in values for values in bindings.values())):
            parameters["response_format"] = {"type": "json_object"}
        if "reasoning_effort" in supported:
            parameters["reasoning_effort"] = "high" if selected == "precise_generation" else "low"
        return parameters
    if family == "gpt":
        effort = "high" if selected == "precise_generation" else "low"
        return {
            "reasoning_effort": effort,
            "max_completion_tokens": GENERATION_OUTPUT_TOKEN_LIMIT,
            "response_format": {"type": "json_object"},
        }

    parameters: dict[str, object] = {
        "max_tokens": GENERATION_OUTPUT_TOKEN_LIMIT,
        "response_format": {"type": "json_object"},
    }
    if selected == "precise_generation":
        parameters["reasoning_effort"] = "high"
        parameters["extra_body"] = {"thinking": {"type": "enabled"}}
    else:
        temperatures = {
            "fast_draft": 0.3,
            "diverse_draft": 0.75,
            "format_repair": 0.1,
        }
        parameters["temperature"] = temperatures[selected]
        parameters["extra_body"] = {"thinking": {"type": "disabled"}}
    return parameters


def generation_parameter_preview(
    provider: str, model: str, preset_id: str | None, model_capabilities: dict | None = None,
) -> dict[str, object]:
    parameters = generation_request_parameters(provider, model, preset_id, model_capabilities)
    if not parameters:
        return {"api_parameters_applied": False, "mode": "기본 API 설정"}
    return {"api_parameters_applied": True, **parameters}


def has_api_key(provider: ProviderName) -> bool:
    if provider_backend(provider) == "bizrouter":
        return has_bizrouter_api_key()
    return has_deepseek_api_key() if provider_backend(provider) == "deepseek" else has_chatkhu_api_key()


def can_gateway_call(provider: ProviderName) -> bool:
    config = DEFAULT_PROVIDERS.get(provider)
    return bool(has_api_key(provider) and (config is None or config.gateway_supported))


def provider_label(provider: str) -> str:
    if provider.startswith(BIZROUTER_PREFIX):
        return f"BizRouter · {provider_model(provider)}"
    config = DEFAULT_PROVIDERS.get(provider)
    if config:
        return config.label
    return LEGACY_PROVIDER_LABELS.get(provider, provider)


def parse_bizrouter_models(raw: str) -> list[dict]:
    try:
        models = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(models, list):
        return []
    result = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if (not isinstance(model_id, str) or not model_id.strip()
                or model_id.startswith("bizrouter/")
                or not isinstance(model.get("input_modalities"), list)
                or "text" not in model["input_modalities"]
                or model.get("output_modalities") != ["text"]):
            continue
        item = {"id": model_id, "input_modalities": ["text"], "output_modalities": ["text"]}
        for key in ("supported_parameters", "response_format_modes"):
            values = model.get(key, [])
            item[key] = [value for value in values if isinstance(value, str)] if isinstance(values, list) else []
        bindings = model.get("provider_response_format_modes", {})
        item["provider_response_format_modes"] = {
            name: [value for value in values if isinstance(value, str)]
            for name, values in bindings.items() if isinstance(values, list)
        } if isinstance(bindings, dict) else {}
        limit = model.get("max_output_tokens")
        if isinstance(limit, int) and limit > 0:
            item["max_output_tokens"] = limit
        result[model_id] = item
    return list(result.values())


def bizrouter_capabilities(raw: str, model: str) -> dict | None:
    return next((item for item in parse_bizrouter_models(raw) if item["id"] == model), None)


def list_bizrouter_models() -> list[dict]:
    key = os.getenv("BIZROUTER_API_KEY")
    if not key:
        raise ValueError("BIZROUTER_API_KEY가 설정되지 않았습니다.")
    try:
        response = httpx.get(f"{BIZROUTER_BASE_URL}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("Invalid catalog")
        return parse_bizrouter_models(json.dumps(payload["models"]))
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" (HTTP {status})" if status else ""
        raise ValueError(f"BizRouter 모델 목록 조회에 실패했습니다{suffix}.") from None


def list_chatkhu_models() -> list[str]:
    from openai import OpenAI

    key = os.getenv("CHATKHU_API_KEY")
    if not key:
        raise ValueError("CHATKHU_API_KEY가 설정되지 않았습니다.")
    client = OpenAI(api_key=key, base_url=CHATKHU_BASE_URL)
    return sorted({model.id for model in client.models.list().data})


def get_chatkhu_credits() -> dict:
    key = os.getenv("CHATKHU_API_KEY")
    if not key:
        raise ValueError("CHATKHU_API_KEY가 설정되지 않았습니다.")
    response = httpx.get(
        f"{CHATKHU_BASE_URL}/credits/",
        headers={"Authorization": f"Bearer {key}"},
        timeout=20.0,
    )
    response.raise_for_status()
    return response.json()


def extract_json(raw: str) -> dict:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("응답에서 JSON 객체를 찾지 못했습니다.")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("최상위 JSON 값은 객체여야 합니다.")
    return value


def manual_result(
    provider: ProviderName,
    model: str,
    operation: str,
    raw: str,
    generation_preset: str | None = None,
) -> ProviderResult:
    started = time.perf_counter()
    try:
        parsed = extract_json(raw)
        error = ""
    except Exception as exc:
        parsed = None
        error = str(exc)
    applied_preset = None
    request_parameters: dict[str, object] = {}
    if operation == "generation":
        request_parameters = {"api_parameters_applied": False, "mode": "web_manual"}
        if supports_generation_presets(provider, model):
            applied_preset = generation_preset if generation_preset in GENERATION_PRESETS else DEFAULT_GENERATION_PRESET
    return ProviderResult(
        provider=provider,
        model=model,
        operation=operation,
        raw_response=raw,
        parsed_json=parsed,
        duration_seconds=time.perf_counter() - started,
        error=error,
        generation_preset=applied_preset,
        request_parameters=request_parameters,
    )


def call_provider(
    provider: ProviderName,
    model: str,
    operation: str,
    system_prompt: str,
    user_prompt: str,
    image_paths: list[Path] | None = None,
    generation_preset: str | None = None,
    managed_retries: bool = False,
    model_capabilities: dict | None = None,
    request_timeout: float | None = None,
) -> ProviderResult:
    started = time.perf_counter()
    raw = ""
    input_tokens = None
    output_tokens = None
    applied_preset = None
    request_parameters: dict[str, object] = {}
    error_retryable = False
    error_retry_after = 0
    try:
        if operation.startswith("review_"):
            raise ValueError("자동 검수는 더 이상 지원하지 않습니다.")
        from openai import OpenAI

        config = DEFAULT_PROVIDERS.get(provider)
        if config and not config.gateway_supported:
            raise ValueError(f"{config.label}은 현재 ChatKHU 웹 수동 모드로만 사용할 수 있습니다.")
        backend = provider_backend(provider, model)
        if backend == "bizrouter":
            key = os.getenv("BIZROUTER_API_KEY")
            base_url = BIZROUTER_BASE_URL
            missing_key_message = "BIZROUTER_API_KEY가 없습니다. .env에 BizRouter API 키를 입력하세요."
            if image_paths or operation == "transcription":
                raise ValueError("BizRouter는 텍스트 문제 생성에만 사용합니다. 이미지 전사는 기존 모델을 선택하세요.")
        elif backend == "deepseek":
            key = os.getenv("DEEPSEEK_API_KEY")
            base_url = DEEPSEEK_BASE_URL
            missing_key_message = "DEEPSEEK_API_KEY가 없습니다. DeepSeek Platform에서 키를 발급해 .env에 입력하세요."
        else:
            key = os.getenv("CHATKHU_API_KEY")
            base_url = CHATKHU_BASE_URL
            missing_key_message = "CHATKHU_API_KEY가 없습니다. ChatKHU 웹 수동 모드를 사용하세요."
        if not key:
            raise ValueError(missing_key_message)
        client = OpenAI(api_key=key, base_url=base_url, max_retries=0 if managed_retries else 2,
                        **({"timeout": request_timeout} if request_timeout is not None else {}))
        if image_paths:
            if backend == "deepseek":
                raise ValueError("DeepSeek 텍스트 모델은 PDF 이미지 전사에 사용할 수 없습니다.")
            user_content: str | list[dict] = [{"type": "text", "text": user_prompt}]
            for image_path in image_paths:
                mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
                encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    }
                )
        else:
            user_content = user_prompt
        api_parameters: dict[str, object] = {}
        if operation == "generation" and supports_generation_presets(provider, model):
            applied_preset = (
                generation_preset
                if generation_preset in GENERATION_PRESETS
                else DEFAULT_GENERATION_PRESET
            )
            api_parameters = generation_request_parameters(provider, model, applied_preset, model_capabilities)
            request_parameters = {"api_parameters_applied": bool(api_parameters), **api_parameters}
        messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
        response = client.chat.completions.create(model=model, messages=messages, **api_parameters)
        raw = response.choices[0].message.content or ""
        input_tokens = getattr(response.usage, "prompt_tokens", None)
        output_tokens = getattr(response.usage, "completion_tokens", None)
        parsed = extract_json(raw)
        error = ""
    except Exception as exc:
        parsed = None
        from topik_question_lab.common.media.media_provider import retry_details
        error_retryable, error_retry_after = retry_details(exc)
        error_retryable = error_retryable or type(exc).__name__ in ("APITimeoutError", "APIConnectionError")
        error = f"{type(exc).__name__}: 모델 요청 또는 응답 처리에 실패했습니다."
        message = str(exc)
        if isinstance(exc, ValueError) and (message.startswith(("CHATKHU_API_KEY가 없습니다.", "DEEPSEEK_API_KEY가 없습니다.", "BIZROUTER_API_KEY가 없습니다.", "BizRouter는 텍스트"))
                or message.endswith("은 현재 ChatKHU 웹 수동 모드로만 사용할 수 있습니다.")):
            error = message
        elif re.fullmatch(r"[45]\d{2} invalid parameter", message, flags=re.IGNORECASE):
            error = f"{type(exc).__name__}: {message}"
        elif getattr(exc, "status_code", None):
            error += f" (HTTP {exc.status_code})"

    return ProviderResult(
        provider=provider,
        model=model,
        operation=operation,
        raw_response=raw,
        parsed_json=parsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_seconds=time.perf_counter() - started,
        error=error,
        error_retryable=error_retryable,
        error_retry_after=error_retry_after,
        generation_preset=applied_preset,
        request_parameters=request_parameters,
    )
