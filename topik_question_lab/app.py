from __future__ import annotations

import json
import os
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
from topik_question_lab.models import (
    AnalysisPayload,
    EnrichmentPayload,
    GeneratedQuestion,
    ProviderResult,
    QuestionExample,
    Review,
)
from topik_question_lab.parser import scan_extracted_text
from topik_question_lab.prompts import (
    DEFAULT_ANALYSIS_GUIDE,
    DEFAULT_SYSTEM_PROMPT,
    ENRICHMENT_SYSTEM_PROMPT,
    build_analysis_prompt,
    build_enrichment_prompt,
    build_generation_prompt,
)
from topik_question_lab.providers import (
    CHATKHU_BASE_URL,
    CHATKHU_WEB_URL,
    DEFAULT_PROVIDERS,
    can_gateway_call,
    call_provider,
    get_chatkhu_credits,
    has_chatkhu_api_key,
    list_chatkhu_models,
    manual_result,
    provider_label,
)
from topik_question_lab.storage import Storage
from topik_question_lab.validation import validate_generation_payload, validate_question


DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "topik_lab.db"
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
def get_storage() -> Storage:
    return Storage(DB_PATH)


storage = get_storage()


def initialize_data() -> None:
    if not storage.list_examples() and EXTRACTED_DIR.exists():
        storage.upsert_examples(scan_extracted_text(EXTRACTED_DIR))
    if not storage.get_setting("system_prompt"):
        storage.set_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    if not storage.get_setting("analysis_guide"):
        storage.set_setting("analysis_guide", DEFAULT_ANALYSIS_GUIDE)
    if not storage.get_setting("generation_count"):
        storage.set_setting("generation_count", "10")
    if not storage.get_setting("difficulty"):
        storage.set_setting("difficulty", "TOPIK II 읽기 초반 수준")
    model_migrations = {
        "gemini": {"", "gemini-2.5-flash"},
        "k_exaone": {"", "K-EXAONE"},
    }
    for provider, previous_ids in model_migrations.items():
        setting_key = f"model_{provider}"
        if storage.get_setting(setting_key, "") in previous_ids:
            storage.set_setting(setting_key, DEFAULT_PROVIDERS[provider].model)


initialize_data()


def model_for(provider: str) -> str:
    config = DEFAULT_PROVIDERS[provider]
    saved_model = storage.get_setting(f"model_{provider}", config.model).strip()
    return saved_model or config.model


def prompts_for(operation: str) -> tuple[str, str]:
    examples = storage.list_examples(approved_only=True)
    system_prompt = storage.get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    if operation == "analysis":
        user_prompt = build_analysis_prompt(examples)
    else:
        user_prompt = build_generation_prompt(
            examples,
            storage.get_setting("analysis_guide", DEFAULT_ANALYSIS_GUIDE),
            int(storage.get_setting("generation_count", "10")),
            storage.get_setting("difficulty", "TOPIK II 읽기 초반 수준"),
        )
    return system_prompt, user_prompt


def save_provider_result(result: ProviderResult, system_prompt: str, user_prompt: str) -> tuple[bool, str]:
    run_id = storage.save_run(result, system_prompt, user_prompt)
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
            )
            storage.add_generated_questions(
                run_id,
                result.provider,
                result.model,
                [question.model_dump() for question in questions],
                [[issue.model_dump() for issue in issues] for issues in issue_groups],
            )
            expected = int(storage.get_setting("generation_count", "10"))
            if len(questions) != expected:
                return True, f"저장했지만 요청한 {expected}개 대신 {len(questions)}개가 생성되었습니다."
    except (ValidationError, ValueError, TypeError) as exc:
        return False, f"응답 구조 검증 실패: {exc}"
    return True, "결과를 저장했습니다."


st.sidebar.title("TOPIK Question Lab")
stage = st.sidebar.radio(
    "작업 단계",
    ["1. 기출 데이터", "2. 유형 분석", "3. 프롬프트 작업실", "4. 문제 생성", "5. 검수·비교", "6. 내보내기"],
)

st.sidebar.divider()
st.sidebar.caption("ChatKHU 연결 상태")
gateway_status = "Gateway 연결" if has_chatkhu_api_key() else "웹 수동 모드"
gateway_class = "status-ok" if has_chatkhu_api_key() else "status-warn"
st.sidebar.markdown(f"**ChatKHU** · <span class='{gateway_class}'>{gateway_status}</span>", unsafe_allow_html=True)
st.sidebar.link_button("ChatKHU 열기", CHATKHU_WEB_URL, use_container_width=True)
st.sidebar.caption("비교 모델")
for provider, config in DEFAULT_PROVIDERS.items():
    mode = "웹 전용" if not config.gateway_supported else model_for(provider)
    st.sidebar.write(f"{config.label} · {mode}")


if stage == "1. 기출 데이터":
    st.title("기출 데이터")
    st.caption("원문에서 1·2번을 구조화하고, 검토가 끝난 예시만 모델에 제공합니다.")

    if "enrichment_message" in st.session_state:
        st.success(st.session_state.pop("enrichment_message"))

    examples = storage.list_examples()
    approved_count = sum(example.approved for example in examples)
    enrichment_needed = [
        example
        for example in examples
        if example.answer is None or not example.grammar_point.strip() or not example.rationale.strip()
    ]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("수집 문제", len(examples))
    col2.metric("승인 문제", approved_count)
    col3.metric("시험 회차", len({example.source_exam for example in examples}))
    col4.metric("AI 보완 필요", len(enrichment_needed))

    action1, action2 = st.columns(2)
    if action1.button("텍스트 다시 스캔", use_container_width=True):
        try:
            parsed = scan_extracted_text(EXTRACTED_DIR)
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
            parsed = scan_extracted_text(EXTRACTED_DIR)
            inserted = storage.upsert_examples(parsed)
            st.success(f"변환 완료. {len(parsed)}개를 확인하고 새 문제 {inserted}개를 추가했습니다.")
            with st.expander("변환 로그"):
                st.code(completed.stdout)
            st.rerun()
        except Exception as exc:
            st.error(f"변환 실패: {exc}")

    if enrichment_needed:
        with st.expander(f"AI로 누락 정보 보완 · {len(enrichment_needed)}개", expanded=True):
            st.caption("한 모델에 보완이 필요한 문제만 한 번 요청합니다. 결과는 제안 상태로 저장되며 자동 승인되지 않습니다.")
            provider_keys = list(DEFAULT_PROVIDERS)
            default_provider = provider_keys.index("k_exaone") if "k_exaone" in provider_keys else 0
            enrichment_provider = st.selectbox(
                "보완 모델",
                provider_keys,
                index=default_provider,
                format_func=provider_label,
                key="enrichment-provider",
            )
            enrichment_model = model_for(enrichment_provider)
            enrichment_prompt = build_enrichment_prompt(enrichment_needed)
            st.text_area(
                "모델에 전달할 보완 프롬프트",
                enrichment_prompt,
                height=220,
                disabled=True,
            )
            gateway_col, web_col = st.columns(2)
            if gateway_col.button(
                "Gateway로 제안 받기",
                type="primary",
                use_container_width=True,
                disabled=not can_gateway_call(enrichment_provider),
            ):
                with st.spinner(f"{provider_label(enrichment_provider)}가 {len(enrichment_needed)}개 문제를 분석하고 있습니다..."):
                    result = call_provider(
                        enrichment_provider,
                        enrichment_model,
                        "enrichment",
                        ENRICHMENT_SYSTEM_PROMPT,
                        enrichment_prompt,
                    )
                    ok, message = save_provider_result(result, ENRICHMENT_SYSTEM_PROMPT, enrichment_prompt)
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
                disabled=not raw_enrichment.strip(),
            ):
                result = manual_result(
                    enrichment_provider,
                    enrichment_model,
                    "enrichment",
                    raw_enrichment,
                )
                ok, message = save_provider_result(result, ENRICHMENT_SYSTEM_PROMPT, enrichment_prompt)
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
                + (" · 보완 필요" if example in enrichment_needed else "")
            )
            for example in examples
        }
        example_keys = list(labels)
        default_example_index = next(
            (index for index, example in enumerate(examples) if example in enrichment_needed),
            0,
        )
        selected_key = st.selectbox(
            "검토할 문제",
            example_keys,
            index=default_example_index,
            format_func=labels.get,
        )
        selected = next(example for example in examples if example.source_key == selected_key)
        if selected.enrichment_model:
            confidence = selected.enrichment_confidence or 0
            st.info(f"AI 제안 · {selected.enrichment_model} · 신뢰도 {confidence:.0%}. 원문과 보기를 확인한 뒤 승인하세요.")
        with st.form(f"example-{selected.source_key}"):
            stem = st.text_input("문장", selected.stem)
            choice_columns = st.columns(2)
            choices = []
            for index, choice in enumerate(selected.choices):
                choices.append(choice_columns[index % 2].text_input(f"보기 {index + 1}", choice, key=f"choice-{selected.source_key}-{index}"))
            answer_label = st.selectbox(
                "정답",
                ["미확정", "1", "2", "3", "4"],
                index=selected.answer or 0,
            )
            grammar = st.text_input("문법 포인트", selected.grammar_point)
            rationale = st.text_area("정답·오답 설계 설명", selected.rationale, height=100)
            approved = st.checkbox("학습 예시로 승인", selected.approved)
            submitted = st.form_submit_button("문제 저장", type="primary")
            if submitted:
                answer = None if answer_label == "미확정" else int(answer_label)
                if approved and answer is None:
                    st.error("승인하려면 정답을 먼저 확정해야 합니다.")
                else:
                    updated = selected.model_copy(
                        update={"stem": stem, "choices": choices, "answer": answer, "grammar_point": grammar, "rationale": rationale, "approved": approved}
                    )
                    storage.save_example(updated)
                    st.success("저장했습니다.")
                    st.rerun()
        with st.expander("추출 원문 보기"):
            st.code(selected.raw_text, language=None)


elif stage == "2. 유형 분석":
    st.title("유형 분석")
    st.caption("선택한 모델의 분석을 같은 입력으로 비교한 뒤 공통 분석서를 편집합니다.")
    examples = storage.list_examples(approved_only=True)
    if not examples:
        st.warning("먼저 기출 데이터에서 사용할 예시를 승인하세요.")
    system_prompt, user_prompt = prompts_for("analysis")
    with st.expander("모델에 전달할 프롬프트"):
        st.text_area("System", system_prompt, height=130, disabled=True)
        st.text_area("User", user_prompt, height=300, disabled=True)
        st.code(f"SYSTEM\n{system_prompt}\n\nUSER\n{user_prompt}", language=None)

    st.info("무료 우선: 위 코드 블록을 복사해 ChatKHU에서 모델을 선택하고 실행한 뒤 JSON 응답을 아래에 붙여넣으세요.")
    st.link_button("ChatKHU에서 분석하기", CHATKHU_WEB_URL)

    providers = st.multiselect("ChatKHU Gateway로 자동 분석할 모델", list(DEFAULT_PROVIDERS), default=[p for p in DEFAULT_PROVIDERS if can_gateway_call(p)], format_func=provider_label)
    if st.button("선택한 Gateway 모델로 분석", type="primary", disabled=not examples or not providers):
        callable_providers = [provider for provider in providers if can_gateway_call(provider)]
        missing = [provider_label(provider) for provider in providers if not can_gateway_call(provider)]
        if missing:
            st.warning(f"ChatKHU Gateway 키가 없어 제외됨: {', '.join(missing)}")
        if callable_providers:
            with st.spinner("모델별 분석을 실행하고 있습니다..."):
                with ThreadPoolExecutor(max_workers=len(callable_providers)) as executor:
                    futures = {
                        executor.submit(call_provider, provider, model_for(provider), "analysis", system_prompt, user_prompt): provider
                        for provider in callable_providers
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        ok, message = save_provider_result(result, system_prompt, user_prompt)
                        (st.success if ok else st.error)(f"{provider_label(result.provider)}: {message}")

    st.subheader("ChatKHU 웹 응답 가져오기")
    manual_provider = st.selectbox("ChatKHU에서 선택한 모델", list(DEFAULT_PROVIDERS), format_func=provider_label, key="analysis-manual-provider")
    raw_analysis = st.text_area("ChatKHU의 JSON 응답", height=180, key="analysis-manual-raw")
    if st.button("분석 응답 저장", disabled=not raw_analysis.strip() or not examples):
        result = manual_result(manual_provider, model_for(manual_provider), "analysis", raw_analysis)
        ok, message = save_provider_result(result, system_prompt, user_prompt)
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
    guide = st.text_area("모든 모델에 동일하게 전달됩니다", storage.get_setting("analysis_guide", DEFAULT_ANALYSIS_GUIDE), height=280)
    if st.button("공통 분석서 저장"):
        storage.set_setting("analysis_guide", guide)
        st.success("저장했습니다.")


elif stage == "3. 프롬프트 작업실":
    st.title("프롬프트 작업실")
    st.caption("모델에 실제로 전달될 지시문과 생성 조건을 버전처럼 보관합니다.")
    with st.form("prompt-settings"):
        system_prompt = st.text_area("시스템 지시문", storage.get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT), height=220)
        analysis_guide = st.text_area("공통 유형 분석서", storage.get_setting("analysis_guide", DEFAULT_ANALYSIS_GUIDE), height=260)
        col1, col2 = st.columns(2)
        count = col1.number_input("모델별 생성 문제 수", min_value=2, max_value=40, step=2, value=int(storage.get_setting("generation_count", "10")))
        difficulty = col2.text_input("목표 난이도", storage.get_setting("difficulty", "TOPIK II 읽기 초반 수준"))
        submitted = st.form_submit_button("프롬프트 설정 저장", type="primary")
        if submitted:
            storage.set_setting("system_prompt", system_prompt)
            storage.set_setting("analysis_guide", analysis_guide)
            storage.set_setting("generation_count", str(count))
            storage.set_setting("difficulty", difficulty)
            version_id = storage.save_prompt_version(system_prompt, analysis_guide, int(count), difficulty)
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
                    st.success(f"버전 #{version['id']}을 복원했습니다.")
                    st.rerun()

    st.subheader("모델 ID")
    st.caption("ChatKHU 계정에서 동기화된 모델 ID입니다. 조직의 허용 목록이 바뀌면 여기에서 수정할 수 있습니다.")
    with st.form("model-settings"):
        model_values = {}
        cols = st.columns(2)
        for index, (provider, config) in enumerate(DEFAULT_PROVIDERS.items()):
            model_values[provider] = cols[index % 2].text_input(config.label, model_for(provider))
        if st.form_submit_button("모델 ID 저장"):
            for provider, value in model_values.items():
                storage.set_setting(f"model_{provider}", value.strip() or DEFAULT_PROVIDERS[provider].model)
            st.success("저장했습니다.")

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

    preview_system, preview_user = prompts_for("generation")
    with st.expander("최종 생성 프롬프트 미리보기", expanded=True):
        st.text_area("System prompt", preview_system, height=160, disabled=True)
        st.text_area("User prompt", preview_user, height=420, disabled=True)


elif stage == "4. 문제 생성":
    st.title("문제 생성")
    examples = storage.list_examples(approved_only=True)
    expected_count = int(storage.get_setting("generation_count", "10"))
    st.caption(f"승인 예시 {len(examples)}개로 모델별 {expected_count}개를 생성합니다. Gateway 실행 버튼을 누를 때만 크레딧을 사용합니다.")
    system_prompt, user_prompt = prompts_for("generation")
    with st.expander("이번 실행 프롬프트"):
        st.text_area("System", system_prompt, height=130, disabled=True)
        st.text_area("User", user_prompt, height=420, disabled=True)
        st.code(f"SYSTEM\n{system_prompt}\n\nUSER\n{user_prompt}", language=None)

    st.info("권장 무료 흐름: 프롬프트를 복사하고 ChatKHU에서 비교할 모델을 각각 선택해 실행한 뒤 응답을 가져오세요.")
    st.link_button("ChatKHU에서 문제 생성하기", CHATKHU_WEB_URL)

    selected_providers = st.multiselect("ChatKHU Gateway로 자동 생성할 모델", list(DEFAULT_PROVIDERS), default=[p for p in DEFAULT_PROVIDERS if can_gateway_call(p)], format_func=provider_label)
    st.caption(f"선택 모델 {len(selected_providers)}개 · 예상 Gateway 요청 {sum(can_gateway_call(p) for p in selected_providers)}회")
    if st.button("선택한 Gateway 모델로 생성", type="primary", disabled=not examples or not selected_providers):
        callable_providers = [provider for provider in selected_providers if can_gateway_call(provider)]
        missing = [provider_label(provider) for provider in selected_providers if not can_gateway_call(provider)]
        if missing:
            st.warning(f"ChatKHU Gateway 키가 없어 제외됨: {', '.join(missing)}")
        if callable_providers:
            with st.spinner("모델별 문제를 생성하고 있습니다..."):
                with ThreadPoolExecutor(max_workers=len(callable_providers)) as executor:
                    futures = {
                        executor.submit(call_provider, provider, model_for(provider), "generation", system_prompt, user_prompt): provider
                        for provider in callable_providers
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        ok, message = save_provider_result(result, system_prompt, user_prompt)
                        (st.success if ok else st.error)(f"{provider_label(result.provider)}: {message}")

    st.subheader("ChatKHU 웹 응답 가져오기")
    manual_provider = st.selectbox("ChatKHU에서 선택한 모델", list(DEFAULT_PROVIDERS), format_func=provider_label, key="generation-manual-provider")
    raw_generation = st.text_area("ChatKHU의 JSON 응답", height=230, key="generation-manual-raw")
    if st.button("생성 응답 검증·저장", disabled=not raw_generation.strip() or not examples):
        result = manual_result(manual_provider, model_for(manual_provider), "generation", raw_generation)
        ok, message = save_provider_result(result, system_prompt, user_prompt)
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
            st.code(result.raw_response or "(빈 응답)", language="json")


elif stage == "5. 검수·비교":
    st.title("검수·비교")
    items = storage.list_generated()
    if not items:
        st.info("아직 생성된 문제가 없습니다.")
    else:
        providers_present = sorted({item["provider"] for item in items})
        selected_provider = st.selectbox("모델 필터", ["전체", *providers_present], format_func=lambda p: "전체" if p == "전체" else provider_label(p))
        filtered = items if selected_provider == "전체" else [item for item in items if item["provider"] == selected_provider]
        labels = {item["id"]: f"#{item['id']} · {provider_label(item['provider'])} · {item['question']['stem']}" for item in filtered}
        selected_id = st.selectbox("검수할 문제", list(labels), format_func=labels.get)
        item = next(item for item in filtered if item["id"] == selected_id)
        question = GeneratedQuestion.model_validate(item["question"])

        if item["validation"]:
            for issue in item["validation"]:
                (st.error if issue["severity"] == "error" else st.warning)(issue["message"])
        else:
            st.success("자동 형식 검사 통과")

        with st.form(f"edit-question-{item['id']}"):
            type_slot = st.selectbox("유형", [1, 2], index=question.type_slot - 1)
            stem = st.text_input("문장", question.stem)
            cols = st.columns(2)
            choices = [cols[index % 2].text_input(f"보기 {index + 1}", choice) for index, choice in enumerate(question.choices)]
            answer = st.selectbox("정답", [1, 2, 3, 4], index=question.answer - 1)
            target_grammar = st.text_input("목표 문법", question.target_grammar)
            explanation = st.text_area("해설", question.explanation, height=120)
            difficulty = st.text_input("난이도", question.difficulty)
            if st.form_submit_button("문제 수정 저장"):
                edited = GeneratedQuestion(
                    type_slot=type_slot,
                    stem=stem,
                    choices=choices,
                    answer=answer,
                    target_grammar=target_grammar,
                    explanation=explanation,
                    difficulty=difficulty,
                )
                other_stems = [current["question"]["stem"] for current in items if current["id"] != item["id"]]
                issues = validate_question(edited, storage.list_examples(approved_only=True), other_stems)
                storage.save_generated_edit(item["id"], edited.model_dump())
                storage.save_validation(item["id"], [issue.model_dump() for issue in issues])
                st.success("수정 내용과 재검사 결과를 저장했습니다.")
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
            if st.form_submit_button("평가 저장", type="primary"):
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
                st.success("평가를 저장했습니다.")
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
