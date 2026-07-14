from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import httpx

from .models import ProviderName, ProviderResult


CHATKHU_BASE_URL = os.getenv("CHATKHU_BASE_URL", "https://factchat-cloud.mindlogic.ai/v1/gateway").rstrip("/")
CHATKHU_WEB_URL = os.getenv("CHATKHU_WEB_URL", "https://chat.khu.ac.kr")


@dataclass(frozen=True)
class ProviderConfig:
    name: ProviderName
    label: str
    model: str
    gateway_supported: bool = True


# These are initial suggestions only. The models enabled for ChatKHU can differ by
# account, so the UI can fetch and display the tenant's actual model list.
DEFAULT_PROVIDERS = {
    "gpt_5_3_chat": ProviderConfig("gpt_5_3_chat", "GPT 5.3 Chat", "gpt-5.3-chat-latest"),
    "claude": ProviderConfig("claude", "Claude Haiku 4.5", "claude-haiku-4-5-20251001"),
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
}

LEGACY_PROVIDER_LABELS = {
    "gpt_5_1": "GPT 5.1 (기존 기록)",
    "gpt_5_2": "GPT 5.2 (기존 기록)",
    "openai": "GPT (기존 기록)",
    "deepseek": "DeepSeek (기존 기록)",
}


def has_chatkhu_api_key() -> bool:
    return bool(os.getenv("CHATKHU_API_KEY"))


def has_api_key(provider: ProviderName) -> bool:
    del provider
    return has_chatkhu_api_key()


def can_gateway_call(provider: ProviderName) -> bool:
    config = DEFAULT_PROVIDERS.get(provider)
    return bool(has_chatkhu_api_key() and config and config.gateway_supported)


def provider_label(provider: str) -> str:
    config = DEFAULT_PROVIDERS.get(provider)
    if config:
        return config.label
    return LEGACY_PROVIDER_LABELS.get(provider, provider)


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


def manual_result(provider: ProviderName, model: str, operation: str, raw: str) -> ProviderResult:
    started = time.perf_counter()
    try:
        parsed = extract_json(raw)
        error = ""
    except Exception as exc:
        parsed = None
        error = str(exc)
    return ProviderResult(
        provider=provider,
        model=model,
        operation=operation,
        raw_response=raw,
        parsed_json=parsed,
        duration_seconds=time.perf_counter() - started,
        error=error,
    )


def call_provider(
    provider: ProviderName,
    model: str,
    operation: str,
    system_prompt: str,
    user_prompt: str,
) -> ProviderResult:
    started = time.perf_counter()
    raw = ""
    input_tokens = None
    output_tokens = None
    try:
        from openai import OpenAI

        config = DEFAULT_PROVIDERS.get(provider)
        if config and not config.gateway_supported:
            raise ValueError(f"{config.label}은 현재 ChatKHU 웹 수동 모드로만 사용할 수 있습니다.")
        key = os.getenv("CHATKHU_API_KEY")
        if not key:
            raise ValueError("CHATKHU_API_KEY가 없습니다. ChatKHU 웹 수동 모드를 사용하세요.")
        client = OpenAI(api_key=key, base_url=CHATKHU_BASE_URL)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = response.choices[0].message.content or ""
        input_tokens = getattr(response.usage, "prompt_tokens", None)
        output_tokens = getattr(response.usage, "completion_tokens", None)
        parsed = extract_json(raw)
        error = ""
    except Exception as exc:
        parsed = None
        error = f"{type(exc).__name__}: {exc}"

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
    )
