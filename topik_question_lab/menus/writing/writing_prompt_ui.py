"""Writing type analysis and prompt editing stages."""
import json

import streamlit as st

from topik_question_lab.common.generation.providers import (
    DEFAULT_PROVIDERS, SERVICE_LABELS, bizrouter_capabilities, can_gateway_call,
    manual_result, parse_bizrouter_models, provider_backend, provider_label, provider_model,
)
from topik_question_lab.common.ui.provider_ui import render_bizrouter_sync
from topik_question_lab.menus.writing import writing_pipeline as pipeline
from topik_question_lab.menus.writing.writing_prompts import (
    build_analysis_prompts, prompt_settings, save_prompt_settings,
)
from topik_question_lab.menus.writing.writing_settings import generation_default


def render_provider(storage, *, prefix="writing", operation="생성", use_saved_default=False):
    saved = generation_default(storage) if use_saved_default else None
    saved_service = provider_backend(saved["provider"]) if saved else next(iter(SERVICE_LABELS))
    if saved:
        signature = (str(storage.path.resolve()), saved["provider"], saved["model"])
        signature_key = f"{prefix}-default-applied"
        if st.session_state.get(signature_key) != signature:
            # Apply changed defaults once; keep per-run choices on ordinary reruns.
            for key in list(st.session_state):
                if key == f"{prefix}-service" or key.startswith((
                    f"{prefix}-provider-", f"{prefix}-model-", f"{prefix}-custom-model-",
                )):
                    st.session_state.pop(key)
            st.session_state[signature_key] = signature
    service = st.selectbox(f"{operation} 서비스", list(SERVICE_LABELS), format_func=SERVICE_LABELS.get,
                           index=list(SERVICE_LABELS).index(saved_service), key=f"{prefix}-service")
    if service == "bizrouter":
        render_bizrouter_sync(storage, key=f"{prefix}-bizrouter-sync")
        options = ["bizrouter:" + item["id"] for item in parse_bizrouter_models(storage.get_setting("bizrouter_models"))]
    else:
        options = [value for value in DEFAULT_PROVIDERS if provider_backend(value) == service]
    preferred = saved["provider"] if saved and service == saved_service else None
    if preferred and preferred not in options:
        options.insert(0, preferred)
    if options:
        provider = st.selectbox("모델", options, index=options.index(preferred) if preferred else 0,
                                format_func=provider_label, key=f"{prefix}-provider-{service}")
        initial_model = saved["model"] if saved and provider == saved["provider"] else provider_model(provider)
        model = st.text_input("모델 ID", initial_model, key=f"{prefix}-model-{provider}").strip()
        if service == "bizrouter":
            provider = "bizrouter:" + model
    else:
        st.info("모델 목록을 동기화하거나 모델 ID를 입력하세요.")
        model = st.text_input("모델 ID", key=f"{prefix}-custom-model-{service}").strip()
        provider = "bizrouter:" + model
    capabilities = bizrouter_capabilities(storage.get_setting("bizrouter_models"), model) if service == "bizrouter" else None
    if not can_gateway_call(provider):
        st.info("선택한 서비스의 API 키를 설정하면 실행할 수 있습니다. 프롬프트를 외부에서 실행한 뒤 응답을 붙여넣을 수도 있습니다.")
    return provider, model, capabilities


def select_references(references, slot, *, prefix):
    candidates = sorted((r for r in references if r.question.type_slot == slot), key=lambda r: -r.source_exam)
    keys = st.multiselect("참고할 기출", [r.source_key for r in candidates],
                          default=[r.source_key for r in candidates],
                          format_func=lambda value: f"{value.split(':')[0]}회 {value.split(':')[1]}번",
                          key=f"{prefix}-references-{slot}")
    return [r for r in candidates if r.source_key in keys]


def render_analysis(storage, references, slot):
    st.subheader(f"{slot}번 공통 유형 분석")
    st.caption("해당 유형의 전체 기출을 종합해 공통 구조·필수 조건·변형 가능한 요소를 정리합니다. "
               "분석서는 새로운 소재의 문제를 만드는 지침으로 사용합니다.")
    selected = [r for r in references if r.question.type_slot == slot]
    exams = sorted({r.source_exam for r in selected})
    can_analyze = len(exams) >= 2
    if can_analyze:
        st.caption(f"분석 자료: 전체 {len(exams)}회차 · {len(selected)}문항 · "
                   + ", ".join(f"{exam}회" for exam in exams))
        system, user = build_analysis_prompts(slot, selected)
        with st.expander("분석 프롬프트 확인"):
            st.code(f"SYSTEM\n{system}\n\nUSER\n{user}", language=None)
    else:
        system, user = "", ""
        st.info("공통 유형 분석에는 서로 다른 두 회차 이상의 기출이 필요합니다.")
    provider, model, capabilities = render_provider(storage, prefix="writing-analysis", operation="분석", use_saved_default=True)
    result = None
    if st.button("공통 유형 분석 실행", type="primary", key=f"writing-analyze-{slot}",
                 disabled=not can_analyze or not model or not can_gateway_call(provider)):
        with st.spinner(f"{slot}번 전체 기출을 종합해 공통 유형 분석 중…"):
            result = pipeline.call_provider(provider, model, "analysis", system, user, model_capabilities=capabilities)
    with st.expander("외부 모델 분석 응답 붙여넣기"):
        raw = st.text_area("분석 응답 JSON", height=180, key=f"writing-analysis-json-{slot}")
        if st.button("분석 응답 저장", key=f"writing-analysis-import-{slot}", disabled=not raw.strip() or not can_analyze):
            result = manual_result(provider, model or "manual", "analysis", raw)
    guide_key = f"writing-analysis-guide-{slot}"
    if result is not None:
        outcome = pipeline.save_analysis_result(storage, result, system, user, slot)
        if outcome["error"]:
            st.error(f"유형 분석서를 변경하지 않았습니다: {outcome['error']}")
        else:
            st.session_state[guide_key] = prompt_settings(storage, slot)["analysis_guide"]
            st.success(f"{slot}번 공통 유형 분석서에 반영했습니다. 아래에서 수정할 수 있습니다.")
    settings = prompt_settings(storage, slot)
    if guide_key not in st.session_state:
        st.session_state[guide_key] = settings["analysis_guide"]
    guide = st.text_area("공통 유형 분석서", height=300, key=guide_key)
    if st.button("공통 유형 분석서 저장", key=f"writing-analysis-save-{slot}", disabled=not guide.strip()):
        save_prompt_settings(storage, slot, {**settings, "analysis_guide": guide.strip()})
        st.success(f"{slot}번 공통 유형 분석서를 저장했습니다.")
    with st.expander("최근 유형 분석 이력"):
        runs = [r for r in storage.list_runs("analysis") if r["question_type"] == f"writing_pbt_{slot}"][:10]
        if not runs:
            st.info("아직 저장된 분석 실행이 없습니다.")
        for run in runs:
            result_data = json.loads(run["result_json"])
            st.caption(f"실행 #{run['id']} · {run['created_at']} · {run['model']}")
            if result_data.get("error"):
                st.warning(result_data["error"])
            else:
                st.write(result_data["parsed_json"]["analysis_guide"])


def render_prompt_workshop(storage, references, slot):
    st.subheader(f"{slot}번 프롬프트 작업실")
    st.caption("이 유형의 설정만 저장됩니다. 문제 생성에서는 저장한 설정에 이번 문항의 소재와 추가 요청을 반영합니다.")
    settings = prompt_settings(storage, slot)
    with st.form(f"writing-prompt-form-{slot}"):
        system = st.text_area("시스템 프롬프트", settings["system"], height=280, key=f"writing-prompt-system-{slot}")
        guide = st.text_area("공통 유형 분석서", settings["analysis_guide"], height=240, key=f"writing-prompt-guide-{slot}")
        difficulty = st.text_input("목표 난이도", settings["difficulty"], key=f"writing-prompt-difficulty-{slot}")
        instructions = st.text_area("유형별 생성 지침", settings["instructions"], height=160,
                                    placeholder="예: 두 빈칸이 각각 문의와 요청 기능을 갖도록 구성해 주세요.",
                                    key=f"writing-prompt-instructions-{slot}")
        if slot == 53:
            image_style = st.text_area("자료 이미지 디자인 지침", settings["image_style"], height=260,
                                      key="writing-prompt-image-style-53")
            st.caption("60·83·102회 기출의 제목 띠·자료 배치·화살표·작은 삽화 구성을 반영했습니다. "
                       "변경한 지침은 다음 이미지 생성부터 적용됩니다.")
        if st.form_submit_button("프롬프트 설정 저장", type="primary"):
            if not system.strip() or not guide.strip() or not difficulty.strip():
                st.error("시스템 프롬프트·유형 분석서·목표 난이도를 입력하세요.")
            else:
                settings = {"system": system.strip(), "analysis_guide": guide.strip(),
                            "difficulty": difficulty.strip(), "instructions": instructions.strip()}
                if slot == 53:
                    settings["image_style"] = image_style.strip()
                save_prompt_settings(storage, slot, settings)
                st.success(f"{slot}번 프롬프트 설정을 저장했습니다.")
    with st.expander("저장된 프롬프트 미리보기"):
        system, user = pipeline.build_prompts([slot], "", references, settings=settings)
        st.caption("실제 생성 소재는 ‘4. 문제 생성’에서 배정합니다.")
        st.code(f"SYSTEM\n{system}\n\nUSER\n{user}", language=None)
