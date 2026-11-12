"""Service selection and catalog sync shared by the generation pages."""
from __future__ import annotations

import json

import streamlit as st

from topik_question_lab.common.generation.providers import SERVICE_LABELS, has_bizrouter_api_key, list_bizrouter_models, provider_backend


def render_bizrouter_sync(storage, *, key: str):
    st.caption(f"BizRouter · {'키 설정됨' if has_bizrouter_api_key() else '.env에 BIZROUTER_API_KEY를 설정하세요.'}")
    if st.button("BizRouter 텍스트 모델 동기화", key=key, disabled=not has_bizrouter_api_key()):
        try:
            models = list_bizrouter_models()
        except ValueError as exc:
            st.error(str(exc))
        else:
            storage.set_setting("bizrouter_models", json.dumps(models, ensure_ascii=False))
            st.rerun()


def generation_service(storage, saved: str, *, key: str) -> str:
    labels = {"all": "모든 서비스", **SERVICE_LABELS}
    service = st.selectbox("생성 서비스", list(labels),
                           index=list(labels).index(saved) if saved in labels else 0,
                           format_func=labels.get, key=key)
    if service != saved:
        storage.set_setting("generation_service", service)
    return service


def service_providers(providers: list[str], service: str) -> list[str]:
    return [provider for provider in providers if service == "all" or provider_backend(provider) == service]
