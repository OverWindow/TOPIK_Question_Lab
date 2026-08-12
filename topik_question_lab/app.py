from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from topik_question_lab.exports import approved_items, to_csv, to_json, to_txt
from topik_question_lab.comparison_pdf import build_comparison_pdf, build_comparison_rows
from topik_question_lab.highlights import (
    has_valid_highlight,
    highlighted_html,
    parse_highlight_marker,
    stem_with_highlight_marker,
    text_with_highlight_marker,
)
from topik_question_lab.models import (
    AnalysisPayload,
    EnrichmentPayload,
    GeneratedQuestion,
    ProviderResult,
    QuestionExample,
    Review,
)
from topik_question_lab.navigation import next_sequence_item
from topik_question_lab.parser import scan_extracted_text
from topik_question_lab.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    ENRICHMENT_SYSTEM_PROMPT,
    build_analysis_prompt,
    build_enrichment_prompt,
    build_generation_prompt,
)
from topik_question_lab.prompt_profiles import (
    DEFAULT_PROVIDER_INSTRUCTIONS,
    QUESTION_TYPE_PROFILES,
    apply_provider_instruction,
    default_analysis_guide,
    question_role_label,
    question_type_profile,
)
from topik_question_lab.providers import (
    CHATKHU_BASE_URL,
    CHATKHU_WEB_URL,
    DEEPSEEK_API_KEYS_URL,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_WEB_URL,
    DEFAULT_ACTIVE_PROVIDERS,
    DEFAULT_PROVIDERS,
    GENERATION_PRESETS,
    can_gateway_call,
    call_provider,
    get_chatkhu_credits,
    generation_parameter_preview,
    generation_preset_prompt,
    has_chatkhu_api_key,
    has_deepseek_api_key,
    list_chatkhu_models,
    manual_result,
    parse_generation_presets,
    provider_backend,
    provider_label,
    resolve_generation_preset,
    dump_generation_presets,
)
from topik_question_lab.storage import Storage
from topik_question_lab.validation import validate_generation_payload, validate_question


DATA_DIR = ROOT / "data"
LEGACY_DB_PATH = DATA_DIR / "topik_lab.db"
TYPE_DB_DIR = DATA_DIR / "types"
EXTRACTED_DIR = ROOT / "extracted_text"

st.set_page_config(page_title="TOPIK Question Lab", page_icon="문", layout="wide")
st.markdown(
    """
    <style>
    :root { --ink: #202124; --line: #d8dadd; --accent: #176b5b; --warm: #a34b27; }
    .block-container { max-width: 1280px; padding-top: 1.5rem; }
    h1, h2, h3 { letter-spacing: 0; color: var(--ink); }
    [data-testid="stSidebar"] { border-right: 1px solid var(--line); }
    [data-testid="stMetric"] { border-left: 3px solid var(--accent); padding-left: .8rem; }
    .status-ok { color: var(--accent); font-weight: 600; }
    .status-warn { color: var(--warm); font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_storage(type_id: str) -> Storage:
    TYPE_DB_DIR.mkdir(parents=True, exist_ok=True)
    type_path = TYPE_DB_DIR / f"{type_id}.db"
    deletion_marker = TYPE_DB_DIR / f".{type_id}.deleted"
    if (
        type_id == "grammar_blank"
        and not type_path.exists()
        and LEGACY_DB_PATH.exists()
        and not deletion_marker.exists()
    ):
        shutil.copy2(LEGACY_DB_PATH, type_path)
    return Storage(type_path)


st.sidebar.title("TOPIK Question Lab")
implemented_type_ids = [
    type_id for type_id, profile in QUESTION_TYPE_PROFILES.items() if profile.implemented
]
pending_type = st.session_state.pop("_selected_type_after_delete", "")
if pending_type in implemented_type_ids:
    st.session_state["selected-question-type"] = pending_type
selected_type_id = st.sidebar.selectbox(
    "문제 유형",
    implemented_type_ids,
    format_func=lambda value: (
        f"{question_type_profile(value).number_range} · {question_type_profile(value).label}"
    ),
    key="selected-question-type",
)
selected_type_profile = question_type_profile(selected_type_id)
storage = get_storage(selected_type_id)
st.sidebar.caption(f"유형 DB · {selected_type_id}.db")
if "_database_delete_notice" in st.session_state:
    st.sidebar.success(st.session_state.pop("_database_delete_notice"))


def initialize_data() -> None:
    if not storage.list_examples() and EXTRACTED_DIR.exists():
        storage.upsert_examples(
            scan_extracted_text(
                EXTRACTED_DIR,
                selected_type_profile.question_numbers,
                selected_type_id,
            )
        )
    if not storage.get_setting("system_prompt"):
        storage.set_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    if not storage.get_setting("analysis_guide"):
        storage.set_setting("analysis_guide", default_analysis_guide(selected_type_id))
    if not storage.get_setting("generation_count"):
        storage.set_setting("generation_count", str(len(selected_type_profile.question_numbers)))
    if not storage.get_setting("difficulty"):
        storage.set_setting("difficulty", "TOPIK II 읽기 초반 수준")
    if not storage.get_setting("prompt_mode"):
        storage.set_setting("prompt_mode", "optimized")
    storage.set_setting("question_type", selected_type_id)
    model_migrations = {
        "gemini": {"", "gemini-2.5-flash"},
        "k_exaone": {"", "K-EXAONE"},
    }
    for provider, previous_ids in model_migrations.items():
        setting_key = f"model_{provider}"
        if storage.get_setting(setting_key, "") in previous_ids:
            storage.set_setting(setting_key, DEFAULT_PROVIDERS[provider].model)


initialize_data()


def active_provider_ids() -> list[str]:
    raw = storage.get_setting("active_providers", "")
    try:
        saved = json.loads(raw) if raw else DEFAULT_ACTIVE_PROVIDERS
    except json.JSONDecodeError:
        saved = DEFAULT_ACTIVE_PROVIDERS
    known_models = {config.model: provider for provider, config in DEFAULT_PROVIDERS.items()}
    active = [
        known_models.get(str(provider).strip(), str(provider).strip())
        for provider in saved
        if str(provider).strip()
    ]
    return active or list(DEFAULT_ACTIVE_PROVIDERS)


def model_for(provider: str) -> str:
    config = DEFAULT_PROVIDERS.get(provider)
    default_model = config.model if config else provider
    saved_model = storage.get_setting(f"model_{provider}", default_model).strip()
    return saved_model or default_model


def generation_preset_for(provider: str) -> str | None:
    return resolve_generation_preset(
        storage.get_setting("generation_presets", "{}"),
        provider,
        model_for(provider),
    )


def provider_service_name(provider: str) -> str:
    return "DeepSeek 공식 API" if provider_backend(provider, model_for(provider)) == "deepseek" else "ChatKHU"


def provider_web_url(provider: str) -> str:
    return DEEPSEEK_WEB_URL if provider_backend(provider, model_for(provider)) == "deepseek" else CHATKHU_WEB_URL


def default_instruction_for(provider: str) -> str:
    if provider in DEFAULT_PROVIDER_INSTRUCTIONS:
        return DEFAULT_PROVIDER_INSTRUCTIONS[provider]
    model = model_for(provider).lower()
    family_keys = (
        ("gpt-5.6", "gpt_5_6_luna"),
        ("gpt", "gpt_5_3_chat"),
        ("claude", "claude"),
        ("gemini-3.5", "gemini_3_5_flash"),
        ("gemini", "gemini"),
        ("exaone", "k_exaone"),
        ("solar", "solar_pro3"),
        ("llama", "llama"),
        ("gemma", "gemma"),
    )
    for marker, instruction_key in family_keys:
        if marker in model:
            return DEFAULT_PROVIDER_INSTRUCTIONS[instruction_key]
    return "요청된 JSON 구조와 문항 수를 정확히 지키고 JSON 바깥의 설명은 출력하지 마십시오."


def provider_instruction(provider: str) -> str:
    return storage.get_setting(
        f"provider_instruction_{provider}",
        default_instruction_for(provider),
    )


def optimize_prompts(system_prompt: str, user_prompt: str, provider: str | None) -> tuple[str, str]:
    return apply_provider_instruction(
        system_prompt,
        user_prompt,
        provider,
        optimized=storage.get_setting("prompt_mode", "optimized") == "optimized",
        instruction=provider_instruction(provider) if provider else "",
    )


def prompts_for(operation: str, provider: str | None = None) -> tuple[str, str]:
    examples = storage.list_examples(approved_only=True)
    system_prompt = storage.get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    type_id = storage.get_setting("question_type", "grammar_blank")
    if operation == "analysis":
        user_prompt = build_analysis_prompt(examples, type_id)
    else:
        user_prompt = build_generation_prompt(
            examples,
            storage.get_setting("analysis_guide", default_analysis_guide(selected_type_id)),
            int(storage.get_setting("generation_count", "2")),
            storage.get_setting("difficulty", "TOPIK II 읽기 초반 수준"),
            type_id,
        )
    return optimize_prompts(system_prompt, user_prompt, provider)


def generation_prompts_for(provider: str) -> tuple[str, str]:
    system_prompt, user_prompt = prompts_for("generation", provider)
    return generation_preset_prompt(system_prompt, generation_preset_for(provider)), user_prompt


def enrichment_prompts_for(examples: list[QuestionExample], provider: str) -> tuple[str, str]:
    type_id = storage.get_setting("question_type", "grammar_blank")
    return optimize_prompts(
        ENRICHMENT_SYSTEM_PROMPT,
        build_enrichment_prompt(examples, type_id),
        provider,
    )


def has_complete_structure(example: QuestionExample) -> bool:
    if any(not choice.strip() for choice in example.choices):
        return False
    profile = question_type_profile(example.question_type)
    if profile.content_mode == "single_sentence":
        return bool(example.stem.strip())
    if not example.passage.strip():
        return False
    if profile.content_mode == "sentence_insertion":
        if not example.auxiliary_text.strip():
            return False
        if any(marker not in example.passage for marker in ("(①)", "(②)", "(③)", "(④)")):
            return False
    return True


def compose_structured_stem(passage: str, question_prompt: str, auxiliary_text: str) -> str:
    return "\n".join(value.strip() for value in (auxiliary_text, passage, question_prompt) if value.strip())


def save_provider_result(
    result: ProviderResult,
    system_prompt: str,
    user_prompt: str,
    allowed_enrichment_keys: set[str] | None = None,
) -> tuple[bool, str]:
    run_id = storage.save_run(
        result,
        system_prompt,
        user_prompt,
        storage.get_setting("question_type", "grammar_blank"),
        storage.get_setting("prompt_mode", "optimized"),
    )
    if result.error or result.parsed_json is None:
        return False, result.error or "JSON 응답이 없습니다."
    try:
        if result.operation == "analysis":
            AnalysisPayload.model_validate(result.parsed_json)
        elif result.operation == "enrichment":
            payload = EnrichmentPayload.model_validate(result.parsed_json)
            examples_by_key = {example.source_key: example for example in storage.list_examples()}
            updated_count = 0
            for suggestion in payload.enrichments:
                if allowed_enrichment_keys is not None and suggestion.source_key not in allowed_enrichment_keys:
                    continue
                current = examples_by_key.get(suggestion.source_key)
                if current is None:
                    continue
                updates = {}
                if current.answer is None:
                    updates["answer"] = suggestion.answer
                if not current.grammar_point.strip():
                    updates["grammar_point"] = suggestion.grammar_point
                if not current.rationale.strip():
                    updates["rationale"] = suggestion.rationale
                if not updates:
                    continue
                updates["enrichment_model"] = result.model
                updates["enrichment_confidence"] = suggestion.confidence
                storage.save_example(current.model_copy(update=updates))
                updated_count += 1
            return True, f"AI 제안을 {updated_count}개 문제에 채웠습니다. 검토 후 승인하세요."
        else:
            existing_stems = [item["question"]["stem"] for item in storage.list_generated()]
            questions, issue_groups = validate_generation_payload(
                result.parsed_json,
                storage.list_examples(approved_only=True),
                existing_generated_stems=existing_stems,
                question_type=selected_type_id,
            )
            storage.add_generated_questions(
                run_id,
                result.provider,
                result.model,
                [question.model_dump() for question in questions],
                [[issue.model_dump() for issue in issues] for issues in issue_groups],
            )
            expected = int(storage.get_setting("generation_count", "2"))
            if len(questions) != expected:
                return True, f"저장했지만 요청한 {expected}개 대신 {len(questions)}개가 생성되었습니다."
    except (ValidationError, ValueError, TypeError) as exc:
        return False, f"응답 구조 검증 실패: {exc}"
    return True, "결과를 저장했습니다."


def available_provider_options() -> list[str]:
    try:
        synced_models = json.loads(storage.get_setting("chatkhu_models", "[]"))
    except json.JSONDecodeError:
        synced_models = []
    known_models = {config.model: provider for provider, config in DEFAULT_PROVIDERS.items()}
    options = list(DEFAULT_PROVIDERS)
    for model_id in [*synced_models, *active_provider_ids()]:
        option = known_models.get(model_id, model_id)
        if isinstance(option, str) and option.strip() and option not in options:
            options.append(option)
    return options


@st.dialog("모델 선택", width="large")
def show_model_picker() -> None:
    st.caption("현재 문제 유형에서 비교할 ChatKHU 및 외부 API 모델을 고릅니다. 선택 결과는 이 유형 DB에만 저장됩니다.")
    if has_chatkhu_api_key():
        if st.button("ChatKHU 전체 모델 동기화", icon=":material/sync:", use_container_width=True):
            try:
                models = list_chatkhu_models()
                storage.set_setting("chatkhu_models", json.dumps(models, ensure_ascii=False))
                st.success(f"허용 모델 {len(models)}개를 불러왔습니다.")
            except Exception as exc:
                st.error(f"모델 목록 조회 실패: {exc}")
    else:
        st.info("API 키가 없으면 아래 직접 추가란에 ChatKHU 웹의 모델 ID를 입력할 수 있습니다.")
    deepseek_status = "API 연결" if has_deepseek_api_key() else "키 없음"
    st.caption(f"DeepSeek V4 Flash / Pro · {deepseek_status} · 공식 API는 ChatKHU와 별도 과금")

    options = available_provider_options()
    picker_key = f"model-picker-values-{selected_type_id}"
    action_all, action_clear = st.columns(2)
    if action_all.button("전체 선택", icon=":material/select_all:", use_container_width=True):
        st.session_state[picker_key] = options
    if action_clear.button("선택 해제", icon=":material/deselect:", use_container_width=True):
        st.session_state[picker_key] = []
    selected = st.multiselect(
        "사용할 모델",
        options,
        default=[provider for provider in active_provider_ids() if provider in options],
        format_func=provider_label,
        key=picker_key,
        help="입력란에서 모델 이름이나 ID를 검색할 수 있습니다.",
    )
    custom_ids = st.text_area(
        "목록에 없는 모델 ID 직접 추가",
        placeholder="모델 ID를 줄바꿈 또는 쉼표로 구분",
        height=90,
    )
    custom = [value.strip() for value in re.split(r"[,\n]", custom_ids) if value.strip()]
    final_selection = list(dict.fromkeys([*selected, *custom]))
    if st.button(
        "선택 모델 적용",
        type="primary",
        icon=":material/check:",
        use_container_width=True,
        disabled=not final_selection,
    ):
        storage.set_setting("active_providers", json.dumps(final_selection, ensure_ascii=False))
        st.rerun()


@st.dialog("유형 데이터베이스 삭제")
def show_database_manager() -> None:
    existing_type_ids = [
        type_id for type_id in implemented_type_ids if (TYPE_DB_DIR / f"{type_id}.db").exists()
    ]
    if not existing_type_ids:
        st.info("삭제할 유형 데이터베이스가 없습니다.")
        return

    target_type = st.selectbox(
        "삭제할 유형 DB",
        existing_type_ids,
        index=existing_type_ids.index(selected_type_id) if selected_type_id in existing_type_ids else 0,
        format_func=lambda value: (
            f"{question_type_profile(value).number_range} · "
            f"{question_type_profile(value).label} · {value}.db"
        ),
        key="delete-database-target",
    )
    target_path = TYPE_DB_DIR / f"{target_type}.db"
    size_kb = target_path.stat().st_size / 1024 if target_path.exists() else 0
    st.caption(f"삭제 파일 · {target_path.name} · {size_kb:,.1f} KB")
    st.warning(
        "이 DB에 저장된 승인, 유형 분석서, 프롬프트 버전, 모델 실행 결과와 평가는 모두 삭제됩니다. "
        "extracted_text 원문은 삭제하지 않으며, 이 유형을 다시 열면 새 DB에 원문 문제가 재수집됩니다."
    )
    confirmed = st.checkbox("삭제 결과를 이해했으며 이 유형 DB를 삭제합니다.")
    typed_type = st.text_input(
        "확인을 위해 유형 ID 입력",
        placeholder=target_type,
        key="delete-database-confirmation",
    )
    if st.button(
        "유형 DB 영구 삭제",
        type="primary",
        icon=":material/delete_forever:",
        use_container_width=True,
        disabled=not confirmed or typed_type.strip() != target_type,
    ):
        target_storage = get_storage(target_type)
        deleted = target_storage.delete_files()
        (TYPE_DB_DIR / f".{target_type}.deleted").touch()
        get_storage.clear()
        st.session_state["_database_delete_notice"] = (
            f"{target_type}.db를 삭제했습니다. 제거 파일 {len(deleted)}개"
        )
        if target_type == selected_type_id:
            st.session_state["_selected_type_after_delete"] = next(
                type_id for type_id in implemented_type_ids if type_id != target_type
            )
        st.rerun()


active_providers = active_provider_ids()
if st.sidebar.button("모델 선택", icon=":material/tune:", use_container_width=True):
    show_model_picker()
st.sidebar.caption(f"이 유형의 비교 대상 · {len(active_providers)}개")
if st.sidebar.button("유형 DB 관리", icon=":material/database:", use_container_width=True):
    show_database_manager()


stage = st.sidebar.radio(
    "작업 단계",
    ["1. 기출 데이터", "2. 유형 분석", "3. 프롬프트 작업실", "4. 문제 생성", "5. 검수·비교", "6. 내보내기"],
)

st.sidebar.divider()
st.sidebar.caption("모델 API 연결 상태")
gateway_status = "Gateway 연결" if has_chatkhu_api_key() else "웹 수동 모드"
gateway_class = "status-ok" if has_chatkhu_api_key() else "status-warn"
st.sidebar.markdown(f"**ChatKHU** · <span class='{gateway_class}'>{gateway_status}</span>", unsafe_allow_html=True)
deepseek_status = "API 연결" if has_deepseek_api_key() else "키 없음"
deepseek_class = "status-ok" if has_deepseek_api_key() else "status-warn"
st.sidebar.markdown(f"**DeepSeek** · <span class='{deepseek_class}'>{deepseek_status}</span>", unsafe_allow_html=True)
st.sidebar.link_button("ChatKHU 열기", CHATKHU_WEB_URL, use_container_width=True)
st.sidebar.caption("선택된 비교 모델")
for provider in active_providers:
    config = DEFAULT_PROVIDERS.get(provider)
    mode = "웹 전용" if config and not config.gateway_supported else model_for(provider)
    st.sidebar.write(f"{provider_label(provider)} · {mode}")


if stage == "1. 기출 데이터":
    st.title("기출 데이터")
    st.caption(
        f"{selected_type_profile.number_range} {selected_type_profile.label}만 이 유형 DB에 수집합니다. "
        "검토가 끝난 예시만 모델에 제공합니다."
    )

    if "enrichment_message" in st.session_state:
        st.success(st.session_state.pop("enrichment_message"))
    if "source_review_message" in st.session_state:
        st.success(st.session_state.pop("source_review_message"))

    examples = storage.list_examples()
    approved_count = sum(example.approved for example in examples)
    highlight_needed = [
        example
        for example in examples
        if example.question_number in selected_type_profile.highlight_numbers and not has_valid_highlight(example)
    ]
    structure_needed = [example for example in examples if not has_complete_structure(example)]
    enrichment_needed = [
        example
        for example in examples
        if example.answer is None or not example.grammar_point.strip() or not example.rationale.strip()
        if example not in structure_needed
        if example.question_number not in selected_type_profile.highlight_numbers or has_valid_highlight(example)
    ]
    metric_values = [
        ("수집 문제", len(examples)),
        ("승인 문제", approved_count),
        ("시험 회차", len({example.source_exam for example in examples})),
        ("구조 확인 필요", len(structure_needed)),
        ("AI 보완 필요", len(enrichment_needed)),
    ]
    if selected_type_profile.highlight_numbers:
        metric_values.append(("밑줄 지정 필요", len(highlight_needed)))
    metric_columns = st.columns(len(metric_values))
    for column, (label, value) in zip(metric_columns, metric_values):
        column.metric(label, value)

    action1, action2 = st.columns(2)
    if action1.button("텍스트 다시 스캔", use_container_width=True):
        try:
            parsed = scan_extracted_text(
                EXTRACTED_DIR,
                selected_type_profile.question_numbers,
                selected_type_id,
            )
            inserted = storage.upsert_examples(parsed)
            st.success(f"{len(parsed)}개를 확인했고 새 문제 {inserted}개를 추가했습니다.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if action2.button("HTML 변환 후 스캔", use_container_width=True):
        try:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "extract_topik_html_questions.py")],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = scan_extracted_text(
                EXTRACTED_DIR,
                selected_type_profile.question_numbers,
                selected_type_id,
            )
            inserted = storage.upsert_examples(parsed)
            st.success(f"변환 완료. {len(parsed)}개를 확인하고 새 문제 {inserted}개를 추가했습니다.")
            with st.expander("변환 로그"):
                st.code(completed.stdout)
            st.rerun()
        except Exception as exc:
            st.error(f"변환 실패: {exc}")

    if highlight_needed:
        st.warning(
            f"{len(highlight_needed)}개 문제는 원문에서 밑줄 위치가 추출되지 않았습니다. "
            "먼저 아래 검토 화면에서 밑줄을 지정해야 AI 정답 보완에 포함됩니다."
        )

    if structure_needed:
        st.warning(
            f"{len(structure_needed)}개 문제는 보기 또는 지문 구조를 완전히 구분하지 못했습니다. "
            "추출 원문을 보면서 지문과 보기 4개를 수정하세요."
        )

    if enrichment_needed:
        with st.expander(f"AI로 누락 정보 보완 · {len(enrichment_needed)}개", expanded=True):
            st.caption("선택한 문제만 모델에 전달합니다. 결과는 제안 상태로 저장되며 자동 승인되지 않습니다.")
            enrichment_labels = {
                example.source_key: (
                    f"{example.source_exam} · {example.question_number}번"
                    + (f" · {example.question_prompt}" if example.question_prompt else "")
                )
                for example in enrichment_needed
            }
            enrichment_options = list(enrichment_labels)
            enrichment_selection_key = f"enrichment-targets-{selected_type_id}"
            saved_enrichment_selection = st.session_state.get(enrichment_selection_key)
            if saved_enrichment_selection is not None:
                valid_selection = [
                    source_key
                    for source_key in saved_enrichment_selection
                    if source_key in enrichment_labels
                ]
                if valid_selection != saved_enrichment_selection:
                    st.session_state[enrichment_selection_key] = valid_selection
            selected_enrichment_keys = st.multiselect(
                "AI 보완 대상 문제",
                enrichment_options,
                default=enrichment_options,
                format_func=enrichment_labels.get,
                key=enrichment_selection_key,
                placeholder="보완할 문제를 선택하세요",
            )
            selected_enrichment = [
                example for example in enrichment_needed if example.source_key in selected_enrichment_keys
            ]
            st.caption(f"선택 {len(selected_enrichment)}개 / 보완 필요 {len(enrichment_needed)}개")
            provider_keys = active_providers
            default_provider = provider_keys.index("k_exaone") if "k_exaone" in provider_keys else 0
            enrichment_provider = st.selectbox(
                "보완 모델",
                provider_keys,
                index=default_provider,
                format_func=provider_label,
                key=f"enrichment-provider-{selected_type_id}",
            )
            enrichment_model = model_for(enrichment_provider)
            enrichment_system, enrichment_prompt = enrichment_prompts_for(selected_enrichment, enrichment_provider)
            st.text_area(
                "모델에 전달할 보완 프롬프트",
                enrichment_prompt,
                height=220,
                disabled=True,
            )
            gateway_col, web_col = st.columns(2)
            if gateway_col.button(
                "API로 제안 받기",
                type="primary",
                use_container_width=True,
                disabled=not selected_enrichment or not can_gateway_call(enrichment_provider),
            ):
                with st.spinner(f"{provider_label(enrichment_provider)}가 {len(selected_enrichment)}개 문제를 분석하고 있습니다..."):
                    result = call_provider(
                        enrichment_provider,
                        enrichment_model,
                        "enrichment",
                        enrichment_system,
                        enrichment_prompt,
                    )
                    ok, message = save_provider_result(
                        result,
                        enrichment_system,
                        enrichment_prompt,
                        allowed_enrichment_keys=set(selected_enrichment_keys),
                    )
                if ok:
                    st.session_state["enrichment_message"] = message
                    st.rerun()
                else:
                    st.error(message)
            web_col.link_button("ChatKHU에서 실행", CHATKHU_WEB_URL, use_container_width=True)

            raw_enrichment = st.text_area(
                "ChatKHU JSON 응답 붙여넣기",
                height=180,
                key="manual-enrichment-response",
            )
            if st.button(
                "붙여넣은 제안 검증·적용",
                use_container_width=True,
                disabled=not selected_enrichment or not raw_enrichment.strip(),
            ):
                result = manual_result(
                    enrichment_provider,
                    enrichment_model,
                    "enrichment",
                    raw_enrichment,
                )
                ok, message = save_provider_result(
                    result,
                    enrichment_system,
                    enrichment_prompt,
                    allowed_enrichment_keys=set(selected_enrichment_keys),
                )
                if ok:
                    st.session_state["enrichment_message"] = message
                    st.rerun()
                else:
                    st.error(message)

    enrichment_runs = storage.list_runs("enrichment")
    if enrichment_runs:
        with st.expander("최근 AI 보완 실행 기록"):
            run_labels = {
                run["id"]: f"#{run['id']} · {run['model']} · {run['created_at']}"
                for run in enrichment_runs[:10]
            }
            selected_run_id = st.selectbox(
                "실행 기록",
                list(run_labels),
                format_func=run_labels.get,
                key="enrichment-run-history",
            )
            selected_run = next(run for run in enrichment_runs if run["id"] == selected_run_id)
            run_result = ProviderResult.model_validate_json(selected_run["result_json"])
            if run_result.error:
                st.error(run_result.error)
            token_text = f"입력 {run_result.input_tokens or '-'} · 출력 {run_result.output_tokens or '-'}"
            st.caption(f"{token_text} · {run_result.duration_seconds:.1f}초")
            st.text_area("저장된 요청 프롬프트", selected_run["prompt_user"], height=160, disabled=True)
            st.text_area("저장된 원본 응답", run_result.raw_response, height=180, disabled=True)

    if not examples:
        st.warning("가져온 문제가 없습니다.")
    else:
        labels = {
            example.source_key: (
                f"{example.source_exam} · 문제 {example.question_number}"
                + (
                    f" · {question_role_label(example.question_type, example.question_number, example.question_prompt)}"
                    if question_role_label(example.question_type, example.question_number, example.question_prompt)
                    else ""
                )
                + (" · 구조 확인" if example in structure_needed else "")
                + (" · 밑줄 필요" if example in highlight_needed else "")
                + (" · 보완 필요" if example in enrichment_needed else "")
            )
            for example in examples
        }
        example_keys = list(labels)
        default_example_index = next(
            (
                index
                for index, example in enumerate(examples)
                if example in structure_needed or example in highlight_needed or example in enrichment_needed
            ),
            0,
        )
        source_selector_key = f"source-review-selection-{selected_type_id}"
        pending_source_key = st.session_state.pop(
            f"pending-source-review-selection-{selected_type_id}",
            "",
        )
        if pending_source_key in example_keys:
            st.session_state[source_selector_key] = pending_source_key
        selected_key = st.selectbox(
            "검토할 문제",
            example_keys,
            index=default_example_index,
            format_func=labels.get,
            key=source_selector_key,
        )
        st.caption(f"검토 위치 · {example_keys.index(selected_key) + 1} / {len(example_keys)}")
        selected = next(example for example in examples if example.source_key == selected_key)
        if selected.parse_warning:
            st.warning(selected.parse_warning)
        if selected.enrichment_model:
            confidence = selected.enrichment_confidence or 0
            st.info(f"AI 제안 · {selected.enrichment_model} · 신뢰도 {confidence:.0%}. 원문과 보기를 확인한 뒤 승인하세요.")
        selected_requires_highlight = selected.question_number in selected_type_profile.highlight_numbers
        if selected_requires_highlight:
            if has_valid_highlight(selected):
                st.markdown(
                    f"**밑줄 미리보기**  \n{highlighted_html(selected.stem, selected.highlight_text)}",
                    unsafe_allow_html=True,
                )
            else:
                target_name = "문장" if selected_type_profile.content_mode == "single_sentence" else "지문"
                st.info(f"{target_name} 입력란에서 대상 표현을 `[[이렇게]]` 감싸거나 밑줄 대상 표현에 직접 입력하세요.")
        with st.form(f"example-{selected.source_key}"):
            passage = selected.passage
            question_prompt = selected.question_prompt
            auxiliary_text = selected.auxiliary_text
            if selected_type_profile.content_mode == "single_sentence":
                stem = st.text_input(
                    "문장" + (" · [[밑줄 부분]] 표시 가능" if selected_requires_highlight else ""),
                    stem_with_highlight_marker(selected) if selected_requires_highlight else selected.stem,
                )
            else:
                if selected_type_profile.content_mode == "sentence_insertion":
                    auxiliary_text = st.text_area("주어진 문장", selected.auxiliary_text, height=80)
                content_label = {
                    "short_text": "짧은 글·안내문·도표",
                    "sentence_order": "(가)~(라) 문장",
                    "headline": "신문 기사 제목",
                }.get(selected_type_profile.content_mode, "지문·자료")
                if selected_requires_highlight:
                    content_label += " · [[밑줄 부분]] 표시 가능"
                passage_value = selected.passage or selected.stem
                if selected_requires_highlight:
                    passage_value = text_with_highlight_marker(passage_value, selected.highlight_text)
                passage = st.text_area(content_label, passage_value, height=260)
                question_prompt = st.text_input("문항 질문", selected.question_prompt)
                stem = compose_structured_stem(passage, question_prompt, auxiliary_text)
            highlight_text = selected.highlight_text
            if selected_requires_highlight:
                highlight_text = st.text_input("밑줄 대상 표현", selected.highlight_text)
            choice_columns = st.columns(2)
            choices = []
            for index, choice in enumerate(selected.choices):
                choices.append(choice_columns[index % 2].text_input(f"보기 {index + 1}", choice, key=f"choice-{selected.source_key}-{index}"))
            answer_label = st.selectbox(
                "정답",
                ["미확정", "1", "2", "3", "4"],
                index=selected.answer or 0,
            )
            grammar_label = "문법 포인트" if selected_type_profile.content_mode == "single_sentence" else "출제 포인트"
            grammar = st.text_input(grammar_label, selected.grammar_point)
            rationale = st.text_area("정답·오답 설계 설명", selected.rationale, height=100)
            approved = st.checkbox("학습 예시로 승인", selected.approved)
            submitted = st.form_submit_button("저장하고 다음 문제", type="primary")
            if submitted:
                answer = None if answer_label == "미확정" else int(answer_label)
                highlight_error = ""
                if selected_requires_highlight:
                    if selected_type_profile.content_mode == "single_sentence":
                        stem, highlight_text, highlight_error = parse_highlight_marker(stem, highlight_text)
                    else:
                        passage, highlight_text, highlight_error = parse_highlight_marker(passage, highlight_text)
                        stem = compose_structured_stem(passage, question_prompt, auxiliary_text)
                structure_error = ""
                if any(not choice.strip() for choice in choices):
                    structure_error = "비어 있지 않은 보기 4개를 입력하세요."
                elif selected_type_profile.content_mode != "single_sentence" and not passage.strip():
                    structure_error = "지문·자료를 입력하세요."
                elif selected_type_profile.content_mode == "sentence_insertion" and not auxiliary_text.strip():
                    structure_error = "삽입할 주어진 문장을 입력하세요."
                elif selected_type_profile.content_mode == "sentence_insertion" and any(
                    marker not in passage for marker in ("(①)", "(②)", "(③)", "(④)")
                ):
                    structure_error = "지문에 삽입 위치 (①)~(④)를 모두 입력하세요."
                if approved and structure_error:
                    st.error(structure_error)
                elif highlight_error:
                    st.error(highlight_error)
                elif approved and answer is None:
                    st.error("승인하려면 정답을 먼저 확정해야 합니다.")
                else:
                    updated = selected.model_copy(
                        update={
                            "stem": stem,
                            "choices": choices,
                            "answer": answer,
                            "grammar_point": grammar,
                            "rationale": rationale,
                            "approved": approved,
                            "question_type": selected_type_id,
                            "highlight_text": highlight_text,
                            "passage": passage,
                            "question_prompt": question_prompt,
                            "auxiliary_text": auxiliary_text,
                            "parse_warning": structure_error,
                        }
                    )
                    storage.save_example(updated)
                    next_key = next_sequence_item(example_keys, selected_key)
                    if next_key is not None:
                        st.session_state[
                            f"pending-source-review-selection-{selected_type_id}"
                        ] = next_key
                        st.session_state["source_review_message"] = "저장했습니다. 다음 문제로 이동했습니다."
                    else:
                        st.session_state["source_review_message"] = "저장했습니다. 마지막 문제입니다."
                    st.rerun()
        with st.expander("추출 원문 보기"):
            st.code(selected.raw_text, language=None)


elif stage == "2. 유형 분석":
    st.title("유형 분석")
    st.caption("선택한 모델의 분석을 같은 입력으로 비교한 뒤 공통 분석서를 편집합니다.")
    examples = storage.list_examples(approved_only=True)
    if not examples:
        st.warning("먼저 기출 데이터에서 사용할 예시를 승인하세요.")
    preview_provider = st.selectbox(
        "프롬프트 미리보기 모델",
        active_providers,
        format_func=provider_label,
        key=f"analysis-preview-provider-{selected_type_id}",
    )
    system_prompt, user_prompt = prompts_for("analysis", preview_provider)
    with st.expander("모델에 전달할 프롬프트"):
        st.text_area("System", system_prompt, height=130, disabled=True)
        st.text_area("User", user_prompt, height=300, disabled=True)
        st.code(f"SYSTEM\n{system_prompt}\n\nUSER\n{user_prompt}", language=None)

    st.info("무료 우선: 위 코드 블록을 선택 모델의 웹 채팅에서 실행한 뒤 JSON 응답을 아래에 붙여넣으세요.")
    st.link_button(f"{provider_service_name(preview_provider)} 웹에서 분석하기", provider_web_url(preview_provider))

    providers = st.multiselect(
        "API로 자동 분석할 모델",
        active_providers,
        default=[provider for provider in active_providers if can_gateway_call(provider)],
        format_func=provider_label,
    )
    if st.button("선택한 API 모델로 분석", type="primary", disabled=not examples or not providers):
        callable_providers = [provider for provider in providers if can_gateway_call(provider)]
        missing = [provider_label(provider) for provider in providers if not can_gateway_call(provider)]
        if missing:
            st.warning(f"해당 서비스의 API 키가 없어 제외됨: {', '.join(missing)}")
        if callable_providers:
            with st.spinner("모델별 분석을 실행하고 있습니다..."):
                with ThreadPoolExecutor(max_workers=len(callable_providers)) as executor:
                    futures = {}
                    for provider in callable_providers:
                        provider_system, provider_user = prompts_for("analysis", provider)
                        future = executor.submit(
                            call_provider,
                            provider,
                            model_for(provider),
                            "analysis",
                            provider_system,
                            provider_user,
                        )
                        futures[future] = (provider, provider_system, provider_user)
                    for future in as_completed(futures):
                        result = future.result()
                        _, provider_system, provider_user = futures[future]
                        ok, message = save_provider_result(result, provider_system, provider_user)
                        (st.success if ok else st.error)(f"{provider_label(result.provider)}: {message}")

    st.subheader("웹 응답 가져오기")
    manual_provider = preview_provider
    st.caption(f"응답 모델 · {provider_label(manual_provider)}")
    manual_system, manual_user = prompts_for("analysis", manual_provider)
    raw_analysis = st.text_area("모델의 JSON 응답", height=180, key="analysis-manual-raw")
    if st.button("분석 응답 저장", disabled=not raw_analysis.strip() or not examples):
        result = manual_result(manual_provider, model_for(manual_provider), "analysis", raw_analysis)
        ok, message = save_provider_result(result, manual_system, manual_user)
        (st.success if ok else st.error)(message)
        if ok:
            st.rerun()

    st.subheader("분석 결과 비교")
    analysis_runs = storage.list_runs("analysis")
    if not analysis_runs:
        st.info("아직 저장된 분석이 없습니다. 아래 공통 분석서는 직접 편집해도 됩니다.")
    for run in analysis_runs:
        result = ProviderResult.model_validate_json(run["result_json"])
        with st.expander(f"#{run['id']} · {provider_label(result.provider)} · {result.model}"):
            if result.error:
                st.error(result.error)
                st.code(result.raw_response or "(빈 응답)")
            elif result.parsed_json:
                st.json(result.parsed_json)
                analysis_text = json.dumps(result.parsed_json["analysis"], ensure_ascii=False, indent=2)
                if st.button("이 분석을 공통 분석서로 사용", key=f"use-analysis-{run['id']}"):
                    storage.set_setting("analysis_guide", analysis_text)
                    st.success("공통 분석서에 반영했습니다.")
                    st.rerun()

    st.subheader("공통 유형 분석서")
    guide = st.text_area(
        "모든 모델에 동일하게 전달됩니다",
        storage.get_setting("analysis_guide", default_analysis_guide(selected_type_id)),
        height=280,
    )
    if st.button("공통 분석서 저장"):
        storage.set_setting("analysis_guide", guide)
        st.success("저장했습니다.")


elif stage == "3. 프롬프트 작업실":
    st.title("프롬프트 작업실")
    st.caption("모델에 실제로 전달될 지시문과 생성 조건을 버전처럼 보관합니다.")
    with st.form("prompt-settings"):
        settings_col1, settings_col2 = st.columns(2)
        type_id = selected_type_id
        settings_col1.text_input(
            "문제 유형 DB",
            f"{selected_type_profile.number_range} · {selected_type_profile.label}",
            disabled=True,
        )
        mode_values = ["optimized", "standard"]
        current_mode = storage.get_setting("prompt_mode", "optimized")
        prompt_mode = settings_col2.radio(
            "프롬프트 적용 방식",
            mode_values,
            index=mode_values.index(current_mode) if current_mode in mode_values else 0,
            format_func=lambda value: "모델별 최적화" if value == "optimized" else "공통 프롬프트",
            horizontal=True,
        )
        system_prompt = st.text_area("시스템 지시문", storage.get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT), height=220)
        analysis_guide = st.text_area(
            "공통 유형 분석서",
            storage.get_setting("analysis_guide", default_analysis_guide(selected_type_id)),
            height=260,
        )
        col1, col2 = st.columns(2)
        slot_count = len(selected_type_profile.question_numbers)
        saved_count = int(storage.get_setting("generation_count", str(slot_count)))
        if saved_count < slot_count or saved_count % slot_count:
            saved_count = slot_count
        count = col1.number_input(
            "모델별 생성 문제 수",
            min_value=slot_count,
            max_value=max(40, slot_count),
            step=slot_count,
            value=saved_count,
        )
        difficulty = col2.text_input("목표 난이도", storage.get_setting("difficulty", "TOPIK II 읽기 초반 수준"))
        submitted = st.form_submit_button("프롬프트 설정 저장", type="primary")
        if submitted:
            storage.set_setting("question_type", type_id)
            storage.set_setting("prompt_mode", prompt_mode)
            storage.set_setting("system_prompt", system_prompt)
            storage.set_setting("analysis_guide", analysis_guide)
            storage.set_setting("generation_count", str(count))
            storage.set_setting("difficulty", difficulty)
            version_id = storage.save_prompt_version(
                system_prompt,
                analysis_guide,
                int(count),
                difficulty,
                type_id,
                prompt_mode,
            )
            st.success(f"프롬프트 버전 #{version_id}로 저장했습니다.")

    versions = storage.list_prompt_versions()
    if versions:
        with st.expander("프롬프트 버전 기록"):
            for version in versions[:10]:
                cols = st.columns([1, 2, 2])
                cols[0].write(f"#{version['id']}")
                cols[1].write(version["created_at"])
                cols[2].write(f"{version['generation_count']}개 · {version['difficulty']}")
                if st.button("복원", key=f"restore-prompt-{version['id']}"):
                    storage.set_setting("system_prompt", version["system_prompt"])
                    storage.set_setting("analysis_guide", version["analysis_guide"])
                    storage.set_setting("generation_count", str(version["generation_count"]))
                    storage.set_setting("difficulty", version["difficulty"])
                    storage.set_setting("question_type", selected_type_id)
                    storage.set_setting("prompt_mode", version["prompt_mode"])
                    st.success(f"버전 #{version['id']}을 복원했습니다.")
                    st.rerun()

    st.subheader("모델별 실행 지침")
    instruction_provider = st.selectbox(
        "조정할 모델",
        active_providers,
        format_func=provider_label,
        key=f"provider-instruction-model-{selected_type_id}",
    )
    instruction_value = st.text_area(
        "추가 지침",
        provider_instruction(instruction_provider),
        height=110,
        key=f"provider-instruction-{instruction_provider}",
    )
    instruction_col1, instruction_col2 = st.columns(2)
    if instruction_col1.button("모델 지침 저장", use_container_width=True):
        storage.set_setting(f"provider_instruction_{instruction_provider}", instruction_value.strip())
        st.success(f"{provider_label(instruction_provider)} 지침을 저장했습니다.")
    if instruction_col2.button("기본 지침 복원", use_container_width=True):
        storage.set_setting(
            f"provider_instruction_{instruction_provider}",
            default_instruction_for(instruction_provider),
        )
        st.rerun()

    st.subheader("모델 ID")
    st.caption("ChatKHU 동기화 모델과 외부 API 모델 ID입니다. 필요한 경우 여기에서 수정할 수 있습니다.")
    with st.form("model-settings"):
        model_values = {}
        cols = st.columns(2)
        for index, provider in enumerate(active_providers):
            model_values[provider] = cols[index % 2].text_input(provider_label(provider), model_for(provider))
        if st.form_submit_button("모델 ID 저장"):
            for provider, value in model_values.items():
                storage.set_setting(f"model_{provider}", value.strip() or model_for(provider))
            st.success("저장했습니다.")

    st.subheader("모델별 생성 프리셋")
    st.caption(
        "읽기 유형과 모델별로 저장되며 문제 생성에만 적용됩니다. "
        "웹 수동 생성은 지침만 복사되고 API 파라미터는 적용되지 않습니다."
    )
    saved_presets = parse_generation_presets(storage.get_setting("generation_presets", "{}"))
    preset_values = {}
    with st.form(f"generation-preset-settings-{selected_type_id}"):
        for provider in active_providers:
            model_id = model_for(provider)
            selected_preset = generation_preset_for(provider)
            st.markdown(f"**{provider_label(provider)}** · `{model_id}`")
            if selected_preset is None:
                st.caption("프리셋 미지원 · 기본 API 설정")
                st.json(generation_parameter_preview(provider, model_id, None))
                continue
            preset_ids = list(GENERATION_PRESETS)
            preset_values[provider] = st.selectbox(
                "생성 프리셋",
                preset_ids,
                index=preset_ids.index(selected_preset),
                format_func=lambda value: GENERATION_PRESETS[value].label,
                key=f"generation-preset-{selected_type_id}-{provider}",
            )
            preset = GENERATION_PRESETS[preset_values[provider]]
            st.caption(preset.description)
            st.json(generation_parameter_preview(provider, model_id, preset_values[provider]))
        if st.form_submit_button("모델별 프리셋 저장", type="primary"):
            saved_presets.update(preset_values)
            storage.set_setting("generation_presets", dump_generation_presets(saved_presets))
            st.success("이 읽기 유형의 모델별 생성 프리셋을 저장했습니다.")

    st.subheader("ChatKHU Gateway")
    st.code(CHATKHU_BASE_URL, language=None)
    if has_chatkhu_api_key():
        gateway_col1, gateway_col2 = st.columns(2)
        if gateway_col1.button("허용 모델 목록 동기화", use_container_width=True):
            try:
                available_models = list_chatkhu_models()
                storage.set_setting("chatkhu_models", json.dumps(available_models, ensure_ascii=False))
                st.success(f"ChatKHU에서 사용 가능한 모델 {len(available_models)}개를 확인했습니다.")
                st.rerun()
            except Exception as exc:
                st.error(f"모델 목록 조회 실패: {exc}")
        if gateway_col2.button("남은 크레딧 확인", use_container_width=True):
            try:
                credits = get_chatkhu_credits()
                st.session_state["chatkhu_credits"] = credits
            except Exception as exc:
                st.error(f"크레딧 조회 실패: {exc}")
        saved_models = json.loads(storage.get_setting("chatkhu_models", "[]"))
        if saved_models:
            with st.expander(f"허용 모델 {len(saved_models)}개"):
                st.code("\n".join(saved_models), language=None)
        if "chatkhu_credits" in st.session_state:
            credits = st.session_state["chatkhu_credits"]
            total = credits.get("total", {})
            st.metric("남은 ChatKHU 크레딧", total.get("remaining", "확인 불가"))
    else:
        st.info("Gateway 자동 호출은 선택 사항입니다. 키 없이도 ChatKHU 웹 복사·붙여넣기 방식으로 전체 기능을 사용할 수 있습니다.")
        st.link_button("ChatKHU 웹 열기", CHATKHU_WEB_URL)

    st.subheader("DeepSeek 공식 API")
    st.code(DEEPSEEK_BASE_URL, language=None)
    if has_deepseek_api_key():
        st.success("DEEPSEEK_API_KEY가 설정되어 DeepSeek V4 Flash와 Pro를 자동 호출할 수 있습니다.")
    else:
        st.info("DeepSeek API 키는 ChatKHU 키와 별도입니다. 키 발급 후 .env의 DEEPSEEK_API_KEY에 입력하세요.")
    st.link_button("DeepSeek API 키 발급", DEEPSEEK_API_KEYS_URL)

    preview_provider = st.selectbox(
        "최종 프롬프트 미리보기 모델",
        active_providers,
        format_func=provider_label,
        key=f"workshop-preview-provider-{selected_type_id}",
    )
    preview_system, preview_user = generation_prompts_for(preview_provider)
    with st.expander("최종 생성 프롬프트 미리보기", expanded=True):
        st.text_area("System prompt", preview_system, height=160, disabled=True)
        st.text_area("User prompt", preview_user, height=420, disabled=True)


elif stage == "4. 문제 생성":
    st.title("문제 생성")
    examples = storage.list_examples(approved_only=True)
    expected_count = int(storage.get_setting("generation_count", "2"))
    st.caption(f"승인 예시 {len(examples)}개로 모델별 {expected_count}개를 생성합니다. API 실행 버튼을 누를 때만 크레딧 또는 API 잔액을 사용합니다.")
    preview_provider = st.selectbox(
        "프롬프트·웹 응답 모델",
        active_providers,
        format_func=provider_label,
        key=f"generation-preview-provider-{selected_type_id}",
    )
    system_prompt, user_prompt = generation_prompts_for(preview_provider)
    with st.expander("이번 실행 프롬프트"):
        st.text_area("System", system_prompt, height=130, disabled=True)
        st.text_area("User", user_prompt, height=420, disabled=True)
        st.code(f"SYSTEM\n{system_prompt}\n\nUSER\n{user_prompt}", language=None)

    st.info("권장 무료 흐름: 프롬프트를 복사하고 선택 모델의 웹 채팅에서 실행한 뒤 응답을 가져오세요. DeepSeek 공식 API 자동 호출은 별도 과금됩니다.")
    st.link_button(f"{provider_service_name(preview_provider)} 웹에서 문제 생성하기", provider_web_url(preview_provider))

    selected_providers = st.multiselect(
        "API로 자동 생성할 모델",
        active_providers,
        default=[provider for provider in active_providers if can_gateway_call(provider)],
        format_func=provider_label,
    )
    for provider in selected_providers:
        preset_id = generation_preset_for(provider)
        if preset_id:
            st.caption(f"{provider_label(provider)} · {GENERATION_PRESETS[preset_id].label}")
        else:
            st.caption(f"{provider_label(provider)} · 프리셋 미지원 · 기본 API 설정")
    st.caption(f"선택 모델 {len(selected_providers)}개 · 예상 API 요청 {sum(can_gateway_call(p) for p in selected_providers)}회")
    if st.button("선택한 API 모델로 생성", type="primary", disabled=not examples or not selected_providers):
        callable_providers = [provider for provider in selected_providers if can_gateway_call(provider)]
        missing = [provider_label(provider) for provider in selected_providers if not can_gateway_call(provider)]
        if missing:
            st.warning(f"해당 서비스의 API 키가 없어 제외됨: {', '.join(missing)}")
        if callable_providers:
            with st.spinner("모델별 문제를 생성하고 있습니다..."):
                with ThreadPoolExecutor(max_workers=len(callable_providers)) as executor:
                    futures = {}
                    for provider in callable_providers:
                        provider_system, provider_user = generation_prompts_for(provider)
                        provider_preset = generation_preset_for(provider)
                        future = executor.submit(
                            call_provider,
                            provider,
                            model_for(provider),
                            "generation",
                            provider_system,
                            provider_user,
                            None,
                            provider_preset,
                        )
                        futures[future] = (provider, provider_system, provider_user)
                    for future in as_completed(futures):
                        result = future.result()
                        _, provider_system, provider_user = futures[future]
                        ok, message = save_provider_result(result, provider_system, provider_user)
                        (st.success if ok else st.error)(f"{provider_label(result.provider)}: {message}")

    st.subheader("웹 응답 가져오기")
    manual_provider = preview_provider
    st.caption(f"응답 모델 · {provider_label(manual_provider)}")
    manual_system, manual_user = generation_prompts_for(manual_provider)
    raw_generation = st.text_area("모델의 JSON 응답", height=230, key="generation-manual-raw")
    if st.button("생성 응답 검증·저장", disabled=not raw_generation.strip() or not examples):
        result = manual_result(
            manual_provider,
            model_for(manual_provider),
            "generation",
            raw_generation,
            generation_preset_for(manual_provider),
        )
        ok, message = save_provider_result(result, manual_system, manual_user)
        (st.success if ok else st.error)(message)
        if ok:
            st.rerun()

    st.subheader("최근 실행")
    for run in storage.list_runs("generation")[:8]:
        result = ProviderResult.model_validate_json(run["result_json"])
        cols = st.columns([2, 2, 1, 1, 3])
        cols[0].write(provider_label(result.provider))
        cols[1].write(result.model)
        cols[2].write(f"{result.duration_seconds:.1f}s")
        cols[3].write(f"{result.input_tokens or '-'} / {result.output_tokens or '-'}")
        cols[4].write(result.error or "저장 완료")
        with st.expander(f"실행 #{run['id']} 원본 응답"):
            if result.generation_preset:
                preset = GENERATION_PRESETS.get(result.generation_preset)
                st.write(f"생성 프리셋 · {preset.label if preset else result.generation_preset}")
            if result.request_parameters:
                st.json(result.request_parameters)
            st.code(result.raw_response or "(빈 응답)", language="json")


elif stage == "5. 검수·비교":
    st.title("검수·비교")
    if "generated_review_message" in st.session_state:
        st.success(st.session_state.pop("generated_review_message"))
    items = storage.list_generated()
    if not items:
        st.info("아직 생성된 문제가 없습니다.")
    else:
        providers_present = sorted({item["provider"] for item in items})
        selected_provider = st.selectbox("모델 필터", ["전체", *providers_present], format_func=lambda p: "전체" if p == "전체" else provider_label(p))
        filtered = items if selected_provider == "전체" else [item for item in items if item["provider"] == selected_provider]
        labels = {
            item["id"]: (
                f"#{item['id']} · {provider_label(item['provider'])}"
                + (
                    f" · {question_role_label(selected_type_id, item['question']['type_slot'], item['question'].get('question_prompt', ''))}"
                    if question_role_label(selected_type_id, item['question']['type_slot'], item['question'].get('question_prompt', ''))
                    else ""
                )
                + f" · {item['question']['stem']}"
            )
            for item in filtered
        }
        generated_ids = list(labels)
        generated_selector_key = f"generated-review-selection-{selected_type_id}"
        pending_generated_id = st.session_state.pop(
            f"pending-generated-review-selection-{selected_type_id}",
            None,
        )
        if pending_generated_id in generated_ids:
            st.session_state[generated_selector_key] = pending_generated_id
        elif st.session_state.get(generated_selector_key) not in generated_ids:
            st.session_state[generated_selector_key] = generated_ids[0]
        selected_id = st.selectbox(
            "검수할 문제",
            generated_ids,
            format_func=labels.get,
            key=generated_selector_key,
        )
        st.caption(f"검수 위치 · {generated_ids.index(selected_id) + 1} / {len(generated_ids)}")
        item = next(item for item in filtered if item["id"] == selected_id)
        question = GeneratedQuestion.model_validate(item["question"])

        if item["validation"]:
            for issue in item["validation"]:
                (st.error if issue["severity"] == "error" else st.warning)(issue["message"])
        else:
            st.success("자동 형식 검사 통과")

        question_requires_highlight = question.type_slot in selected_type_profile.highlight_numbers
        if question_requires_highlight and question.highlight_text:
            st.markdown(
                f"**밑줄 미리보기**  \n{highlighted_html(question.stem, question.highlight_text)}",
                unsafe_allow_html=True,
            )

        with st.form(f"edit-question-{item['id']}"):
            slot_options = list(selected_type_profile.question_numbers)
            type_slot = st.selectbox(
                "문제 번호 슬롯",
                slot_options,
                index=slot_options.index(question.type_slot) if question.type_slot in slot_options else 0,
                format_func=lambda slot: (
                    f"{slot}번 · {question_role_label(selected_type_id, slot)}"
                    if question_role_label(selected_type_id, slot)
                    else f"{slot}번"
                ),
            )
            passage = question.passage
            question_prompt = question.question_prompt
            auxiliary_text = question.auxiliary_text
            set_id = question.set_id
            if selected_type_profile.content_mode == "single_sentence":
                stem = st.text_input("문장", question.stem)
            else:
                if selected_type_profile.shared_passage:
                    set_id = st.text_input("공통 지문 세트 ID", question.set_id)
                    st.caption("같은 실행 기록과 세트 ID를 가진 문항의 지문은 함께 갱신됩니다.")
                if selected_type_profile.content_mode == "sentence_insertion":
                    auxiliary_text = st.text_area("주어진 문장", question.auxiliary_text, height=80)
                content_label = {
                    "short_text": "짧은 글·안내문·도표",
                    "sentence_order": "(가)~(라) 문장",
                    "headline": "신문 기사 제목",
                }.get(selected_type_profile.content_mode, "지문·자료")
                if question_requires_highlight:
                    content_label += " · [[밑줄 부분]] 표시 가능"
                passage_value = question.passage or question.stem
                if question_requires_highlight:
                    passage_value = text_with_highlight_marker(passage_value, question.highlight_text)
                passage = st.text_area(content_label, passage_value, height=260)
                question_prompt = st.text_input("문항 질문", question.question_prompt)
                stem = compose_structured_stem(passage, question_prompt, auxiliary_text)
            generated_highlight = question.highlight_text
            if question_requires_highlight:
                generated_highlight = st.text_input("밑줄 대상 표현", question.highlight_text)
            cols = st.columns(2)
            choices = [cols[index % 2].text_input(f"보기 {index + 1}", choice) for index, choice in enumerate(question.choices)]
            answer = st.selectbox("정답", [1, 2, 3, 4], index=question.answer - 1)
            target_grammar = st.text_input("목표 문법", question.target_grammar)
            explanation = st.text_area("해설", question.explanation, height=120)
            difficulty = st.text_input("난이도", question.difficulty)
            if st.form_submit_button("문제 수정 저장"):
                highlight_error = ""
                if question_requires_highlight:
                    if selected_type_profile.content_mode == "single_sentence":
                        stem, generated_highlight, highlight_error = parse_highlight_marker(stem, generated_highlight)
                    else:
                        passage, generated_highlight, highlight_error = parse_highlight_marker(passage, generated_highlight)
                        stem = compose_structured_stem(passage, question_prompt, auxiliary_text)
                if highlight_error:
                    st.error(highlight_error)
                    st.stop()
                edited = GeneratedQuestion(
                    type_slot=type_slot,
                    stem=stem,
                    question_type=selected_type_id,
                    highlight_text=generated_highlight,
                    passage=passage,
                    question_prompt=question_prompt,
                    auxiliary_text=auxiliary_text,
                    set_id=set_id,
                    choices=choices,
                    answer=answer,
                    target_grammar=target_grammar,
                    explanation=explanation,
                    difficulty=difficulty,
                )
                other_stems = [current["question"]["stem"] for current in items if current["id"] != item["id"]]
                issues = validate_question(edited, storage.list_examples(approved_only=True), other_stems)
                storage.save_generated_edit(item["id"], edited.model_dump())
                sibling_ids = []
                if selected_type_profile.shared_passage:
                    sibling_ids = storage.sync_generated_set_passage(item["id"], set_id, passage)
                storage.save_validation(item["id"], [issue.model_dump() for issue in issues])
                suffix = f" 공통 지문 문항 {len(sibling_ids)}개도 동기화했습니다." if sibling_ids else ""
                st.success(f"수정 내용과 재검사 결과를 저장했습니다.{suffix}")
                st.rerun()

        review = Review.model_validate(item["review"])
        st.subheader("사람 평가")
        with st.form(f"review-{item['id']}"):
            cols = st.columns(4)
            naturalness = cols[0].slider("자연스러움", 1, 5, review.naturalness)
            difficulty_fit = cols[1].slider("난이도 적합성", 1, 5, review.difficulty_fit)
            distractor_quality = cols[2].slider("오답 품질", 1, 5, review.distractor_quality)
            topik_fit = cols[3].slider("TOPIK 적합성", 1, 5, review.topik_fit)
            notes = st.text_area("검수 메모", review.notes)
            approved = st.checkbox("최종 승인", review.approved)
            if st.form_submit_button("평가 저장하고 다음 문제", type="primary"):
                storage.save_review(
                    item["id"],
                    Review(
                        naturalness=naturalness,
                        difficulty_fit=difficulty_fit,
                        distractor_quality=distractor_quality,
                        topik_fit=topik_fit,
                        notes=notes,
                        approved=approved,
                    ),
                )
                next_id = next_sequence_item(generated_ids, selected_id)
                if next_id is not None:
                    st.session_state[
                        f"pending-generated-review-selection-{selected_type_id}"
                    ] = next_id
                    st.session_state["generated_review_message"] = (
                        "평가를 저장했습니다. 다음 문제로 이동했습니다."
                    )
                else:
                    st.session_state["generated_review_message"] = (
                        "평가를 저장했습니다. 마지막 문제입니다."
                    )
                st.rerun()

        st.subheader("모델 비교")
        metrics: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        counts: dict[str, int] = defaultdict(int)
        review_counts: dict[str, int] = defaultdict(int)
        for current in items:
            provider = current["provider"]
            counts[provider] += 1
            current_review = current["review"]
            metrics[provider]["승인율"] += 1 if current_review.get("approved") else 0
            metrics[provider]["오류율"] += 1 if any(issue["severity"] == "error" for issue in current["validation"]) else 0
            if current["reviewed"]:
                review_counts[provider] += 1
                for key, label in [("naturalness", "자연스러움"), ("difficulty_fit", "난이도"), ("distractor_quality", "오답 품질"), ("topik_fit", "TOPIK 적합성")]:
                    metrics[provider][label] += current_review.get(key, 3)
        rows = []
        for provider, values in metrics.items():
            count = counts[provider]
            review_count = review_counts[provider]
            rows.append(
                {
                    "모델": provider_label(provider),
                    "문제 수": count,
                    "평가 수": review_count,
                    "승인율": f"{values['승인율'] / count:.0%}",
                    "형식 오류율": f"{values['오류율'] / count:.0%}",
                    "자연스러움": round(values["자연스러움"] / review_count, 2) if review_count else "-",
                    "난이도": round(values["난이도"] / review_count, 2) if review_count else "-",
                    "오답 품질": round(values["오답 품질"] / review_count, 2) if review_count else "-",
                    "TOPIK 적합성": round(values["TOPIK 적합성"] / review_count, 2) if review_count else "-",
                }
            )
        st.dataframe(rows, use_container_width=True, hide_index=True)


elif stage == "6. 내보내기":
    st.title("내보내기")
    items = storage.list_generated()
    approved = approved_items(items)
    st.metric("내보낼 승인 문제", len(approved))
    if not approved:
        st.warning("검수·비교 단계에서 최종 승인한 문제가 없습니다.")
    else:
        txt_data = to_txt(items)
        json_data = to_json(items)
        csv_data = to_csv(items)
        cols = st.columns(3)
        cols[0].download_button("TXT 받기", txt_data, "topik_generated_questions.txt", "text/plain", use_container_width=True)
        cols[1].download_button("JSON 받기", json_data, "topik_generated_questions.json", "application/json", use_container_width=True)
        cols[2].download_button("CSV 받기", csv_data.encode("utf-8-sig"), "topik_generated_questions.csv", "text/csv", use_container_width=True)
        st.subheader("TXT 미리보기")
        st.code(txt_data, language=None)

    st.divider()
    st.subheader("원본 · 모델 A · 모델 B 비교 PDF")
    st.caption("실제로 생성 기록이 있는 두 모델을 선택하면 같은 문제 번호 슬롯의 원본과 생성 문제를 순서대로 묶어 A4 가로 3열 PDF로 만듭니다.")
    model_identities = sorted(
        {(item["provider"], item["model"]) for item in items},
        key=lambda identity: (provider_label(identity[0]), identity[1]),
    )
    identity_labels = {
        identity: f"{provider_label(identity[0])} · {identity[1]}"
        for identity in model_identities
    }
    if len(model_identities) < 2:
        st.info("비교 PDF를 만들려면 서로 다른 모델의 생성 기록이 두 개 이상 필요합니다.")
    else:
        model_columns = st.columns(2)
        model_a = model_columns[0].selectbox(
            "모델 A",
            model_identities,
            format_func=identity_labels.get,
            key=f"pdf-model-a-{selected_type_id}",
        )
        model_b_options = [identity for identity in model_identities if identity != model_a]
        model_b = model_columns[1].selectbox(
            "모델 B",
            model_b_options,
            format_func=identity_labels.get,
            key=f"pdf-model-b-{selected_type_id}",
        )
        option_columns = st.columns(3)
        approved_sources_only = option_columns[0].checkbox(
            "승인된 원본만",
            value=True,
            key=f"pdf-approved-source-{selected_type_id}",
        )
        approved_generated_only = option_columns[1].checkbox(
            "최종 승인 생성물만",
            value=False,
            key=f"pdf-approved-generated-{selected_type_id}",
        )
        include_answers = option_columns[2].checkbox(
            "정답·해설 포함",
            value=True,
            key=f"pdf-include-answers-{selected_type_id}",
        )
        comparison_rows = build_comparison_rows(
            storage.list_examples(),
            items,
            model_a,
            model_b,
            approved_sources_only=approved_sources_only,
            approved_generated_only=approved_generated_only,
        )
        if not comparison_rows:
            st.warning("현재 필터와 문제 번호 슬롯에서 완성된 3열 비교 묶음을 만들 수 없습니다. 승인 필터를 해제하거나 다른 모델을 선택하세요.")
        else:
            max_rows = len(comparison_rows)
            comparison_count = st.number_input(
                "PDF에 넣을 비교 묶음 수",
                min_value=1,
                max_value=max_rows,
                value=min(10, max_rows),
                step=1,
                key=f"pdf-comparison-count-{selected_type_id}-{max_rows}",
            )
            selected_rows = comparison_rows[: int(comparison_count)]
            st.caption(
                f"완성 가능한 비교 묶음 {max_rows}개 · PDF 포함 {len(selected_rows)}개 · 긴 문항은 연속 페이지로 자동 분할"
            )
            pdf_data = build_comparison_pdf(
                selected_rows,
                f"{selected_type_profile.number_range} {selected_type_profile.label}",
                identity_labels[model_a],
                identity_labels[model_b],
                include_answers=include_answers,
            )
            st.download_button(
                "3열 비교 PDF 받기",
                pdf_data,
                f"topik_comparison_{selected_type_id}.pdf",
                "application/pdf",
                icon=":material/picture_as_pdf:",
                type="primary",
                use_container_width=True,
            )
