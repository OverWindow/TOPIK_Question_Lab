from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(PROJECT_ROOT))).resolve()
load_dotenv(ROOT / ".env")

from topik_question_lab.listening_comparison_pdf import build_listening_comparison_pdf
from topik_question_lab.listening_exports import to_csv, to_json, to_txt, to_zip
from topik_question_lab.listening_importer import (
    apply_answer_map,
    apply_enrichment_payload,
    discover_listening_pdfs,
    exam_name,
    load_imported_examples,
    merge_page_transcription,
    needs_transcription,
    render_pdf,
    render_reference_pdf,
    ready_for_enrichment,
    save_imported_examples,
    save_edited_imported_example,
    validate_import_coverage,
)
from topik_question_lab.listening_models import (
    ChartSpec,
    DialogueTurn,
    GeneratedListeningQuestion,
    ListeningQuestionExample,
    VisualOption,
)
from topik_question_lab.listening_profiles import LISTENING_TYPE_PROFILES, listening_type_profile
from topik_question_lab.listening_prompts import (
    LISTENING_SYSTEM_PROMPT,
    TRANSCRIPTION_SYSTEM_PROMPT,
    build_listening_analysis_prompt,
    build_listening_enrichment_prompt,
    build_listening_generation_prompt,
    build_transcription_prompt,
)
from topik_question_lab.listening_storage import ListeningStorage
from topik_question_lab.listening_validation import validate_listening_payload, validate_listening_question
from topik_question_lab.listening_visuals import render_chart, save_uploaded_asset, suggest_visual_prompt
from topik_question_lab.models import AnalysisPayload, Review
from topik_question_lab.navigation import next_sequence_item
from topik_question_lab.prompt_profiles import DEFAULT_PROVIDER_INSTRUCTIONS, apply_provider_instruction
from topik_question_lab.providers import (
    DEFAULT_ACTIVE_PROVIDERS,
    DEFAULT_PROVIDERS,
    call_provider,
    can_gateway_call,
    has_chatkhu_api_key,
    has_deepseek_api_key,
    list_chatkhu_models,
    manual_result,
    provider_backend,
    provider_label,
)


SOURCE_DIR = ROOT / "TOPIK-II-Listening-Script"
ANSWER_DIR = ROOT / "TOPIK-II-Reading-Test-Paper" / "answers"
IMPORT_ROOT = ROOT / "data" / "listening" / "imports"
ANSWER_IMPORT_ROOT = ROOT / "data" / "listening" / "answer_imports"
TYPE_DB_DIR = ROOT / "data" / "listening" / "types"
ASSET_ROOT = ROOT / "data" / "listening" / "assets"


st.set_page_config(page_title="TOPIK II 듣기 문제 생성 Lab", page_icon="🎧", layout="wide")
st.markdown(
    """
    <style>
    :root { --ink:#202124; --line:#d8dadd; --accent:#176b5b; --warm:#a34b27; }
    .block-container { max-width: 1320px; padding-top: 1.35rem; }
    [data-testid="stSidebar"] { border-right:1px solid var(--line); }
    [data-testid="stMetric"] { border-left:3px solid var(--accent); padding-left:.7rem; }
    .script-box { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fafafa; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_storage(db_path: str) -> ListeningStorage:
    return ListeningStorage(Path(db_path))


def model_for(provider: str) -> str:
    config = DEFAULT_PROVIDERS.get(provider)
    return config.model if config else provider


def active_providers(storage: ListeningStorage) -> list[str]:
    raw = storage.get_setting("active_providers", "")
    try:
        values = json.loads(raw) if raw else list(DEFAULT_ACTIVE_PROVIDERS)
    except json.JSONDecodeError:
        values = list(DEFAULT_ACTIVE_PROVIDERS)
    return [str(value) for value in values if str(value).strip()]


def provider_display(provider: str) -> str:
    label = provider_label(provider)
    backend = "DeepSeek" if provider_backend(provider, model_for(provider)) == "deepseek" else "ChatKHU"
    return f"{label} · {backend}"


def optimized_prompts(storage: ListeningStorage, system: str, user: str, provider: str) -> tuple[str, str]:
    mode = storage.get_setting("prompt_mode", "optimized")
    instruction = storage.get_setting(f"instruction_{provider}", DEFAULT_PROVIDER_INSTRUCTIONS.get(provider, ""))
    return apply_provider_instruction(system, user, provider, mode == "optimized", instruction)


def parse_dialogue(text: str) -> list[DialogueTurn]:
    turns = []
    for line in text.splitlines():
        if not line.strip():
            continue
        speaker, marker, content = line.partition(":")
        if not marker:
            speaker, marker, content = line.partition("：")
        if marker and speaker.strip() and content.strip():
            turns.append(DialogueTurn(speaker=speaker.strip(), text=content.strip()))
    return turns


def dialogue_text(turns: list[DialogueTurn]) -> str:
    return "\n".join(f"{turn.speaker}: {turn.text}" for turn in turns)


st.sidebar.title("TOPIK II 듣기 Lab")
type_id = st.sidebar.selectbox(
    "듣기 문제 유형",
    list(LISTENING_TYPE_PROFILES),
    format_func=lambda value: f"{listening_type_profile(value).number_range}번 · {listening_type_profile(value).label}",
)
profile = listening_type_profile(type_id)
storage = get_storage(str(TYPE_DB_DIR / f"{type_id}.db"))
if not storage.get_setting("analysis_guide"):
    storage.set_setting("analysis_guide", profile.analysis_focus)
if not storage.get_setting("system_prompt"):
    storage.set_setting("system_prompt", LISTENING_SYSTEM_PROMPT)
if not storage.get_setting("generation_count"):
    storage.set_setting("generation_count", str(len(profile.question_numbers)))
if not storage.get_setting("difficulty"):
    storage.set_setting("difficulty", "TOPIK II 듣기")
if not storage.get_setting("prompt_mode"):
    storage.set_setting("prompt_mode", "optimized")

provider_values = active_providers(storage)
with st.sidebar.expander("모델 선택", expanded=False):
    known = storage.get_setting("known_models", "")
    try:
        known_models = json.loads(known) if known else []
    except json.JSONDecodeError:
        known_models = []
    options = list(dict.fromkeys([*DEFAULT_PROVIDERS, *known_models, *provider_values]))
    selected_models = st.multiselect("사용할 모델", options, default=provider_values, format_func=provider_display)
    col_sync, col_save = st.columns(2)
    if col_sync.button("ChatKHU 동기화", use_container_width=True):
        try:
            synced = list_chatkhu_models()
            storage.set_setting("known_models", json.dumps(synced))
            st.success(f"{len(synced)}개 모델 동기화")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if col_save.button("선택 적용", type="primary", use_container_width=True):
        storage.set_setting("active_providers", json.dumps(selected_models))
        st.rerun()

provider_values = active_providers(storage)
st.sidebar.caption(f"듣기 DB · {type_id}.db")
st.sidebar.caption("선택 모델: " + ", ".join(provider_label(value) for value in provider_values))
stage = st.sidebar.radio(
    "작업 단계",
    ["1. 기출 데이터", "2. 유형 분석", "3. 프롬프트 작업실", "4. 문제 생성", "5. 검수·비교", "6. 내보내기"],
)
st.sidebar.divider()
st.sidebar.write(f"ChatKHU: {'API 연결' if has_chatkhu_api_key() else '웹 수동'}")
st.sidebar.write(f"DeepSeek: {'API 연결' if has_deepseek_api_key() else '웹 수동'}")


def sync_type_examples() -> int:
    imported = [value for value in load_imported_examples(IMPORT_ROOT) if value.question_type == type_id]
    return storage.upsert_examples(imported) if imported else 0


if not storage.list_examples() and IMPORT_ROOT.exists():
    sync_type_examples()


if stage == "1. 기출 데이터":
    st.title("기출 데이터")
    st.caption("스캔형 PDF를 페이지 이미지로 보존하고, 전사 결과를 사람이 승인한 뒤에만 생성 예시로 사용합니다.")
    if "listening_source_review_message" in st.session_state:
        st.success(st.session_state.pop("listening_source_review_message"))
    pdfs = discover_listening_pdfs(SOURCE_DIR)
    examples = storage.list_examples()
    metrics = st.columns(5)
    metrics[0].metric("PDF", len(pdfs))
    metrics[1].metric("수집 문항", len(examples))
    metrics[2].metric("승인", sum(value.approved for value in examples))
    metrics[3].metric("전사 필요", sum(needs_transcription(value) for value in examples))
    metrics[4].metric("정답 미확정", sum(value.answer is None for value in examples))

    enrichment_candidates = [value for value in examples if ready_for_enrichment(value)]
    with st.expander(f"AI로 현재 유형 전체 정답·해설 일괄 보완 · {len(enrichment_candidates)}개", expanded=bool(enrichment_candidates)):
        st.caption("대상 문항 전체를 하나의 JSON 요청으로 묶습니다. 실행 버튼 한 번당 API 호출도 한 번만 발생합니다.")
        incomplete_count = sum(value.answer is None for value in examples) - len(enrichment_candidates)
        if incomplete_count:
            st.info(f"대본·발문·보기 구조가 아직 완성되지 않은 {incomplete_count}개 문항은 이번 요청에서 제외됩니다.")
        batch_provider = st.selectbox("일괄 보완 모델", provider_values, format_func=provider_display, key=f"batch-enrichment-provider-{type_id}")
        batch_prompt = build_listening_enrichment_prompt(enrichment_candidates, type_id)
        st.text_area("전체 유형 보완 프롬프트", batch_prompt, height=260, key=f"batch-enrichment-prompt-{type_id}")
        if st.button("현재 유형 전체 일괄 보완 · API 1회", type="primary", disabled=not enrichment_candidates):
            result = call_provider(batch_provider, model_for(batch_provider), "enrichment", LISTENING_SYSTEM_PROMPT, batch_prompt)
            storage.save_run(result, LISTENING_SYSTEM_PROMPT, batch_prompt, type_id, "batch_enrichment")
            if result.error or not result.parsed_json:
                st.error(result.error or "보완 JSON을 얻지 못했습니다.")
            else:
                try:
                    updated, missing = apply_enrichment_payload(enrichment_candidates, result.parsed_json, model_for(batch_provider))
                    for example in updated:
                        if example.source_key not in missing:
                            storage.save_example(example)
                    applied_count = len(updated) - len(missing)
                    if missing:
                        st.warning(f"{applied_count}개를 저장했고 {len(missing)}개 응답이 누락되었습니다. 같은 대상만 다시 실행할 수 있습니다.")
                    else:
                        st.success(f"API 1회로 {applied_count}개 문항의 정답과 해설을 저장했습니다. 검토 후 승인하세요.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        manual_batch = st.text_area("웹 수동 일괄 응답 JSON", height=180, key=f"manual-batch-enrichment-{type_id}")
        if st.button("수동 일괄 응답 적용", disabled=not enrichment_candidates or not manual_batch.strip()):
            result = manual_result(batch_provider, model_for(batch_provider), "enrichment", manual_batch)
            storage.save_run(result, LISTENING_SYSTEM_PROMPT, batch_prompt, type_id, "batch_enrichment_manual")
            if result.error or not result.parsed_json:
                st.error(result.error)
            else:
                try:
                    updated, missing = apply_enrichment_payload(enrichment_candidates, result.parsed_json, model_for(batch_provider))
                    for example in updated:
                        if example.source_key not in missing:
                            storage.save_example(example)
                    st.success(f"{len(updated) - len(missing)}개 문항에 적용했습니다.")
                    if missing:
                        st.warning(f"응답 누락 {len(missing)}개")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    with st.expander("PDF 수집·전사", expanded=not examples):
        st.write(f"원본 폴더에서 {len(pdfs)}개 PDF를 찾았습니다.")
        if st.button("8개 회차 렌더링 및 400개 슬롯 생성", type="primary", disabled=not pdfs):
            progress = st.progress(0)
            try:
                for index, pdf in enumerate(pdfs, start=1):
                    render_pdf(pdf, IMPORT_ROOT)
                    progress.progress(index / len(pdfs), text=pdf.name)
                inserted = sync_type_examples()
                st.success(f"렌더링 완료. 현재 유형에 새 후보 {inserted}개를 추가했습니다.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        manifests = []
        for path in sorted(IMPORT_ROOT.glob("*/manifest.json")):
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        if manifests:
            selected_exam = st.selectbox("전사할 회차", [item["exam"] for item in manifests])
            manifest = next(item for item in manifests if item["exam"] == selected_exam)
            relevant_pages = sorted({value.source_page for value in load_imported_examples(IMPORT_ROOT) if value.source_exam == selected_exam and value.question_type == type_id})
            page_number = st.selectbox("페이지", relevant_pages or list(range(1, manifest["page_count"] + 1)))
            page_image = Path(manifest["pages"][page_number - 1])
            st.image(str(page_image), caption=f"{selected_exam} · {page_number}쪽", width=650)
            transcript_models = [value for value in provider_values if provider_backend(value, model_for(value)) != "deepseek"]
            transcript_provider = st.selectbox("이미지 전사 모델", transcript_models, format_func=provider_display) if transcript_models else None
            prompt = build_transcription_prompt(selected_exam, page_number)
            st.text_area("전사 프롬프트", prompt, height=180)
            if st.button("이 페이지 자동 전사", disabled=not transcript_provider):
                result = call_provider(transcript_provider, model_for(transcript_provider), "transcription", TRANSCRIPTION_SYSTEM_PROMPT, prompt, [page_image])
                storage.save_run(result, TRANSCRIPTION_SYSTEM_PROMPT, prompt, type_id, "multimodal")
                if result.error or not result.parsed_json:
                    st.error(result.error or "전사 JSON을 얻지 못했습니다.")
                else:
                    merge_page_transcription(manifest, result.parsed_json, model_for(transcript_provider))
                    sync_type_examples()
                    st.success("전사 후보를 저장했습니다.")
                    st.rerun()
            if st.button("현재 유형 8회차 미전사 페이지 일괄 전사", disabled=not transcript_provider):
                progress = st.status("미전사 페이지를 처리하고 있습니다.", expanded=True)
                completed, failed = 0, []
                for target_manifest in manifests:
                    imported = [
                        value for value in load_imported_examples(IMPORT_ROOT)
                        if value.source_exam == target_manifest["exam"] and value.question_type == type_id and needs_transcription(value)
                    ]
                    for target_page in sorted({value.source_page for value in imported}):
                        target_image = Path(target_manifest["pages"][target_page - 1])
                        target_prompt = build_transcription_prompt(target_manifest["exam"], target_page)
                        result = call_provider(transcript_provider, model_for(transcript_provider), "transcription", TRANSCRIPTION_SYSTEM_PROMPT, target_prompt, [target_image])
                        storage.save_run(result, TRANSCRIPTION_SYSTEM_PROMPT, target_prompt, type_id, "multimodal")
                        if result.error or not result.parsed_json:
                            failed.append(f"{target_manifest['exam']} {target_page}쪽: {result.error or 'JSON 없음'}")
                            progress.write(f"❌ {target_manifest['exam']} · {target_page}쪽")
                        else:
                            merge_page_transcription(target_manifest, result.parsed_json, model_for(transcript_provider))
                            completed += 1
                            progress.write(f"✅ {target_manifest['exam']} · {target_page}쪽")
                sync_type_examples()
                progress.update(label=f"일괄 전사 완료 · 성공 {completed}, 실패 {len(failed)}", state="complete" if completed else "error")
                for message in failed:
                    st.error(message)
            manual_json = st.text_area("수동 전사 JSON 붙여넣기", height=170, key=f"manual-transcription-{selected_exam}-{page_number}")
            if st.button("수동 JSON 적용", disabled=not manual_json.strip()):
                try:
                    payload = json.loads(manual_json)
                    merge_page_transcription(manifest, payload, "manual")
                    sync_type_examples()
                    st.success("수동 전사를 적용했습니다.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    examples = storage.list_examples()
    coverage = validate_import_coverage(load_imported_examples(IMPORT_ROOT))
    for message in coverage:
        st.warning(message)
    if not examples:
        st.info("먼저 PDF 수집·전사에서 페이지를 렌더링하세요.")
    else:
        labels = {value.source_key: f"{value.source_exam} · {value.question_number}번 · {value.question_role}" for value in examples}
        example_keys = list(labels)
        source_selector_key = f"listening-source-review-selection-{type_id}"
        pending_source_key = st.session_state.pop(
            f"pending-listening-source-review-selection-{type_id}",
            "",
        )
        if pending_source_key in example_keys:
            st.session_state[source_selector_key] = pending_source_key
        selected_key = st.selectbox(
            "검토 문항",
            example_keys,
            format_func=labels.get,
            key=source_selector_key,
        )
        st.caption(f"검토 위치 · {example_keys.index(selected_key) + 1} / {len(example_keys)}")
        selected = next(value for value in examples if value.source_key == selected_key)
        left, right = st.columns([1, 1.15])
        with left:
            if selected.source_page_image and Path(selected.source_page_image).exists():
                st.image(selected.source_page_image, caption=f"원본 {selected.source_page}쪽", use_container_width=True)
        with right:
            with st.form(f"source-{selected.source_key}"):
                source_widget_prefix = f"source-field-{type_id}-{selected.source_key}"
                dialogue = st.text_area(
                    "대본 · '화자: 발화' 형식",
                    dialogue_text(selected.dialogue_turns),
                    height=220,
                    key=f"{source_widget_prefix}-dialogue",
                )
                question_prompt = st.text_input(
                    "발문",
                    selected.question_prompt,
                    key=f"{source_widget_prefix}-question-prompt",
                )
                choices = []
                if profile.visual_kind == "none":
                    cols = st.columns(2)
                    for index in range(4):
                        value = selected.choices[index] if index < len(selected.choices) else ""
                        choices.append(
                            cols[index % 2].text_input(
                                f"보기 {index + 1}",
                                value,
                                key=f"{source_widget_prefix}-choice-{index}",
                            )
                        )
                else:
                    visual_options = []
                    for index in range(4):
                        option = selected.visual_options[index] if index < len(selected.visual_options) else VisualOption(number=index + 1)
                        description = st.text_input(
                            f"시각 선택지 {index + 1} 설명",
                            option.description,
                            key=f"{source_widget_prefix}-visual-description-{index}",
                        )
                        visual_options.append(option.model_copy(update={"description": description}))
                answer_value = st.selectbox(
                    "정답",
                    ["미확정", "1", "2", "3", "4"],
                    index=selected.answer or 0,
                    key=f"{source_widget_prefix}-answer",
                )
                answer_source = st.selectbox(
                    "정답 출처",
                    ["unknown", "official", "ai_suggested", "manual"],
                    index=["unknown", "official", "ai_suggested", "manual"].index(selected.answer_source),
                    key=f"{source_widget_prefix}-answer-source",
                )
                target_skill = st.text_input("출제 포인트", selected.target_skill, key=f"{source_widget_prefix}-target-skill")
                rationale = st.text_area("정답·오답 설명", selected.rationale, height=100, key=f"{source_widget_prefix}-rationale")
                warning = st.text_input("구조 확인 메모", selected.parse_warning, key=f"{source_widget_prefix}-warning")
                approved = st.checkbox("학습 예시로 승인", selected.approved, key=f"{source_widget_prefix}-approved")
                submitted = st.form_submit_button("저장하고 다음 문항", type="primary")
            if submitted:
                answer = None if answer_value == "미확정" else int(answer_value)
                turns = parse_dialogue(dialogue)
                structure_error = ""
                if not turns:
                    structure_error = "대본을 '화자: 발화' 형식으로 입력하세요."
                elif profile.visual_kind == "none" and any(not value.strip() for value in choices):
                    structure_error = "보기 4개가 필요합니다."
                elif profile.visual_kind != "none" and any(not value.description.strip() for value in visual_options):
                    structure_error = "시각 선택지 설명 4개가 필요합니다."
                elif not question_prompt.strip():
                    structure_error = "발문이 필요합니다."
                elif approved and answer is None:
                    structure_error = "승인하려면 정답을 확정하세요."
                if approved and structure_error:
                    st.error(structure_error)
                else:
                    saved_warning = warning.strip() or structure_error
                    update = {
                        "dialogue_turns": turns,
                        "question_prompt": question_prompt,
                        "choices": choices if profile.visual_kind == "none" else [],
                        "visual_options": visual_options if profile.visual_kind != "none" else [],
                        "answer": answer,
                        "answer_source": answer_source,
                        "target_skill": target_skill,
                        "rationale": rationale,
                        "parse_warning": saved_warning,
                        "approved": approved,
                    }
                    edited_example = selected.model_copy(update=update)
                    storage.save_example(edited_example)
                    save_edited_imported_example(IMPORT_ROOT, edited_example)
                    next_key = next_sequence_item(example_keys, selected_key)
                    if next_key is not None:
                        st.session_state[
                            f"pending-listening-source-review-selection-{type_id}"
                        ] = next_key
                        st.session_state["listening_source_review_message"] = (
                            "대본과 문항 내용을 저장했습니다. 다음 문항으로 이동했습니다."
                        )
                    else:
                        st.session_state["listening_source_review_message"] = (
                            "대본과 문항 내용을 저장했습니다. 마지막 문항입니다."
                        )
                    st.rerun()

            with st.expander("정답표 JSON 적용"):
                st.caption('현재 회차 전체 정답을 예: {"1": 2, "2": 4} 형식으로 입력합니다.')
                answer_json = st.text_area("공식 정답 JSON", key=f"answers-{selected.source_exam}")
                if st.button("현재 회차에 공식 정답 적용", disabled=not answer_json.strip()):
                    try:
                        answers = {int(key): int(value) for key, value in json.loads(answer_json).items()}
                        all_imported = load_imported_examples(IMPORT_ROOT)
                        exam_values = [value for value in all_imported if value.source_exam == selected.source_exam]
                        updated = apply_answer_map(exam_values, answers, "official")
                        exam_dir = Path(next(value.source_page_image for value in updated if value.source_page_image)).parent.parent
                        save_imported_examples(exam_dir, updated)
                        sync_type_examples()
                        st.success("공식 정답을 적용했습니다.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                answer_pdfs = sorted(ANSWER_DIR.glob("*.pdf")) if ANSWER_DIR.exists() else []
                if answer_pdfs:
                    answer_pdf = st.selectbox("로컬 답안 PDF", answer_pdfs, format_func=lambda value: value.name, key=f"answer-pdf-{selected.source_exam}")
                    if st.button("선택 답안 PDF 렌더링"):
                        try:
                            render_reference_pdf(answer_pdf, ANSWER_IMPORT_ROOT)
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                    answer_manifest_path = ANSWER_IMPORT_ROOT / answer_pdf.stem.replace(" ", "_") / "manifest.json"
                    if not answer_manifest_path.exists():
                        candidates = list(ANSWER_IMPORT_ROOT.glob(f"*/manifest.json"))
                        answer_manifest_path = next((path for path in candidates if json.loads(path.read_text(encoding="utf-8")).get("source_pdf") == str(answer_pdf.resolve())), answer_manifest_path)
                    if answer_manifest_path.exists():
                        answer_manifest = json.loads(answer_manifest_path.read_text(encoding="utf-8"))
                        answer_page = st.selectbox("답안 페이지", list(range(1, answer_manifest["page_count"] + 1)), key=f"answer-page-{selected.source_exam}")
                        answer_image = Path(answer_manifest["pages"][answer_page - 1])
                        st.image(str(answer_image), width=620)
                        answer_models = [value for value in provider_values if provider_backend(value, model_for(value)) != "deepseek"]
                        answer_provider = st.selectbox("답안 표 인식 모델", answer_models, format_func=provider_display, key=f"answer-model-{selected.source_exam}") if answer_models else None
                        if st.button("듣기 정답 표 자동 인식", disabled=not answer_provider):
                            answer_prompt = """첨부된 TOPIK 답안표에서 '영역: 듣기'에 해당하는 1~50번 정답만 추출하십시오.
읽기 또는 쓰기 영역은 제외하십시오. 듣기 정답표가 이 페이지에 없으면 answers를 빈 객체로 반환하십시오.
다음 JSON만 출력하십시오: {"answers": {"1": 1, "2": 2}}"""
                            result = call_provider(answer_provider, model_for(answer_provider), "transcription", TRANSCRIPTION_SYSTEM_PROMPT, answer_prompt, [answer_image])
                            storage.save_run(result, TRANSCRIPTION_SYSTEM_PROMPT, answer_prompt, type_id, "answer_transcription")
                            if result.error or not result.parsed_json:
                                st.error(result.error or "정답 JSON을 얻지 못했습니다.")
                            else:
                                try:
                                    answers = {int(key): int(value) for key, value in result.parsed_json.get("answers", {}).items()}
                                    if not answers:
                                        raise ValueError("이 페이지에서 듣기 정답표를 찾지 못했습니다.")
                                    all_imported = load_imported_examples(IMPORT_ROOT)
                                    exam_values = [value for value in all_imported if value.source_exam == selected.source_exam]
                                    updated = apply_answer_map(exam_values, answers, "official")
                                    exam_dir = Path(updated[0].source_page_image).parent.parent
                                    save_imported_examples(exam_dir, updated)
                                    sync_type_examples()
                                    st.success(f"공식 정답 {len(answers)}개를 적용했습니다.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(str(exc))

            with st.expander("AI로 정답·해설 제안"):
                enrichment_provider = st.selectbox("제안 모델", provider_values, format_func=provider_display, key=f"enrich-provider-{selected.source_key}")
                enrichment_prompt = f"""다음 TOPIK II 듣기 문항의 대본·발문·보기만 근거로 정답 하나를 판단하십시오.
확신하기 어려우면 confidence를 낮게 지정하십시오.

대본:
{dialogue_text(selected.dialogue_turns)}
발문: {selected.question_prompt}
보기: {json.dumps(selected.choices or [option.description for option in selected.visual_options], ensure_ascii=False)}

다음 JSON만 출력하십시오.
{{"answer": 1, "target_skill": "출제 포인트", "rationale": "정답과 대표 오답 설명", "confidence": 0.8}}"""
                st.text_area("제안 프롬프트", enrichment_prompt, height=210)
                if st.button("AI 제안 실행", disabled=not selected.dialogue_turns):
                    result = call_provider(enrichment_provider, model_for(enrichment_provider), "enrichment", LISTENING_SYSTEM_PROMPT, enrichment_prompt)
                    storage.save_run(result, LISTENING_SYSTEM_PROMPT, enrichment_prompt, type_id, "enrichment")
                    if result.error or not result.parsed_json:
                        st.error(result.error or "제안 JSON을 얻지 못했습니다.")
                    else:
                        try:
                            payload = result.parsed_json
                            updated = selected.model_copy(
                                update={
                                    "answer": int(payload["answer"]),
                                    "answer_source": "ai_suggested",
                                    "target_skill": str(payload.get("target_skill", "")),
                                    "rationale": str(payload.get("rationale", "")),
                                    "approved": False,
                                }
                            )
                            storage.save_example(updated)
                            st.success("AI 제안을 저장했습니다. 검토 후 승인하세요.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))


elif stage == "2. 유형 분석":
    st.title("유형 분석")
    approved = storage.list_examples(approved_only=True)
    st.metric("승인 기출", len(approved))
    prompt = build_listening_analysis_prompt(approved, type_id)
    st.text_area("분석 프롬프트", prompt, height=330)
    provider = st.selectbox("분석 모델", provider_values, format_func=provider_display)
    if st.button("분석 실행", type="primary", disabled=not approved):
        system, user = optimized_prompts(storage, storage.get_setting("system_prompt"), prompt, provider)
        result = call_provider(provider, model_for(provider), "analysis", system, user)
        storage.save_run(result, system, user, type_id, storage.get_setting("prompt_mode"))
        if result.error or not result.parsed_json:
            st.error(result.error or "분석 JSON을 얻지 못했습니다.")
        else:
            analysis = AnalysisPayload.model_validate(result.parsed_json).analysis
            guide = "\n".join(
                [analysis.sentence_structure, *analysis.answer_conditions, *analysis.distractor_rules, analysis.notes]
            ).strip()
            storage.set_setting("analysis_guide", guide)
            st.success("공통 유형 분석서에 반영했습니다.")
            st.rerun()
    manual = st.text_area("웹 수동 응답 JSON", height=160)
    if st.button("수동 분석 저장", disabled=not manual.strip()):
        result = manual_result(provider, model_for(provider), "analysis", manual)
        storage.save_run(result, storage.get_setting("system_prompt"), prompt, type_id, storage.get_setting("prompt_mode"))
        if result.error or not result.parsed_json:
            st.error(result.error)
        else:
            analysis = AnalysisPayload.model_validate(result.parsed_json).analysis
            storage.set_setting("analysis_guide", "\n".join([analysis.sentence_structure, *analysis.answer_conditions, *analysis.distractor_rules, analysis.notes]))
            st.rerun()


elif stage == "3. 프롬프트 작업실":
    st.title("프롬프트 작업실")
    system = st.text_area("시스템 프롬프트", storage.get_setting("system_prompt"), height=180)
    guide = st.text_area("공통 유형 분석서", storage.get_setting("analysis_guide"), height=220)
    col1, col2, col3 = st.columns(3)
    count = col1.number_input("생성 문항 수", min_value=len(profile.question_numbers), max_value=40, value=int(storage.get_setting("generation_count", str(len(profile.question_numbers)))), step=len(profile.question_numbers) if profile.shared_script else 1)
    difficulty = col2.text_input("목표 난이도", storage.get_setting("difficulty", "TOPIK II 듣기"))
    mode = col3.selectbox("프롬프트 모드", ["optimized", "standard"], index=0 if storage.get_setting("prompt_mode") == "optimized" else 1, format_func=lambda value: "모델별 최적화" if value == "optimized" else "공통 프롬프트")
    if st.button("프롬프트 설정 저장", type="primary"):
        storage.set_setting("system_prompt", system)
        storage.set_setting("analysis_guide", guide)
        storage.set_setting("generation_count", str(count))
        storage.set_setting("difficulty", difficulty)
        storage.set_setting("prompt_mode", mode)
        st.success("저장했습니다.")
    approved = storage.list_examples(approved_only=True)
    preview = build_listening_generation_prompt(approved, guide, count, difficulty, type_id)
    st.text_area("최종 사용자 프롬프트 미리보기", preview, height=430)


elif stage == "4. 문제 생성":
    st.title("문제 생성")
    approved = storage.list_examples(approved_only=True)
    count = int(storage.get_setting("generation_count", str(len(profile.question_numbers))))
    guide = storage.get_setting("analysis_guide", profile.analysis_focus)
    difficulty = storage.get_setting("difficulty", "TOPIK II 듣기")
    base_prompt = build_listening_generation_prompt(approved, guide, count, difficulty, type_id)
    st.caption(f"승인 기출 {len(approved)}개 · 선택 모델 {len(provider_values)}개")
    run_targets = st.multiselect("이번 실행 모델", provider_values, default=provider_values, format_func=provider_display)
    if st.button("선택 모델 병행 생성", type="primary", disabled=not approved or not run_targets):
        status = st.status("모델별 생성을 실행하고 있습니다.", expanded=True)
        successes, failures = 0, []
        for provider in run_targets:
            system, user = optimized_prompts(storage, storage.get_setting("system_prompt"), base_prompt, provider)
            result = call_provider(provider, model_for(provider), "generation", system, user)
            run_id = storage.save_run(result, system, user, type_id, storage.get_setting("prompt_mode"))
            if result.error or not result.parsed_json:
                failures.append(f"{provider_label(provider)}: {result.error or 'JSON 없음'}")
                status.write(f"❌ {provider_label(provider)}")
                continue
            try:
                questions, issue_groups = validate_listening_payload(result.parsed_json, approved, type_id)
                storage.add_generated_questions(
                    run_id,
                    provider,
                    model_for(provider),
                    [question.model_dump(mode="json") for question in questions],
                    [[issue.model_dump() for issue in group] for group in issue_groups],
                )
                successes += 1
                status.write(f"✅ {provider_label(provider)} · {len(questions)}문항")
            except Exception as exc:
                failures.append(f"{provider_label(provider)}: {exc}")
                status.write(f"❌ {provider_label(provider)} · {exc}")
        status.update(label=f"완료 · 성공 {successes}, 실패 {len(failures)}", state="complete" if successes else "error")
        for failure in failures:
            st.error(failure)

    st.divider()
    manual_provider = st.selectbox("웹 수동 응답 모델", provider_values, format_func=provider_display)
    manual_system, manual_prompt = optimized_prompts(storage, storage.get_setting("system_prompt"), base_prompt, manual_provider)
    st.text_area("복사용 생성 프롬프트", manual_prompt, height=280)
    manual_response = st.text_area("모델 JSON 응답", height=220)
    if st.button("수동 생성 결과 저장", disabled=not manual_response.strip()):
        result = manual_result(manual_provider, model_for(manual_provider), "generation", manual_response)
        run_id = storage.save_run(result, manual_system, manual_prompt, type_id, storage.get_setting("prompt_mode"))
        if result.error or not result.parsed_json:
            st.error(result.error)
        else:
            try:
                questions, issue_groups = validate_listening_payload(result.parsed_json, approved, type_id)
                storage.add_generated_questions(run_id, manual_provider, model_for(manual_provider), [q.model_dump(mode="json") for q in questions], [[i.model_dump() for i in group] for group in issue_groups])
                st.success(f"{len(questions)}문항을 저장했습니다.")
            except Exception as exc:
                st.error(str(exc))


elif stage == "5. 검수·비교":
    st.title("검수·비교")
    if "listening_generated_review_message" in st.session_state:
        st.success(st.session_state.pop("listening_generated_review_message"))
    items = storage.list_generated()
    if not items:
        st.info("먼저 문제 생성 단계에서 결과를 저장하세요.")
    else:
        labels = {item["id"]: f"#{item['id']} · {provider_label(item['provider'])} · {item['question']['type_slot']}번형" for item in items}
        item_ids = list(labels)
        generated_selector_key = f"listening-generated-review-selection-{type_id}"
        pending_generated_id = st.session_state.pop(
            f"pending-listening-generated-review-selection-{type_id}",
            None,
        )
        if pending_generated_id in item_ids:
            st.session_state[generated_selector_key] = pending_generated_id
        selected_id = st.selectbox(
            "생성 문항",
            item_ids,
            format_func=labels.get,
            key=generated_selector_key,
        )
        st.caption(f"검수 위치 · {item_ids.index(selected_id) + 1} / {len(item_ids)}")
        item = next(value for value in items if value["id"] == selected_id)
        question = GeneratedListeningQuestion.model_validate(item["question"])
        generated_widget_prefix = f"generated-field-{type_id}-{selected_id}"
        left, right = st.columns([1.15, 0.85])
        with left:
            dialogue = st.text_area(
                "대본",
                dialogue_text(question.dialogue_turns),
                height=240,
                key=f"{generated_widget_prefix}-dialogue",
            )
            prompt = st.text_input("발문", question.question_prompt, key=f"{generated_widget_prefix}-question-prompt")
            choices = []
            visual_options = list(question.visual_options)
            if profile.visual_kind == "none":
                cols = st.columns(2)
                for index in range(4):
                    value = question.choices[index] if index < len(question.choices) else ""
                    choices.append(cols[index % 2].text_input(f"보기 {index + 1}", value, key=f"g-choice-{selected_id}-{index}"))
            else:
                asset_dir = ASSET_ROOT / type_id / str(selected_id)
                st.caption("그림·그래프 파일은 선택 사항입니다. 파일이 없어도 생성 프롬프트가 있으면 최종 승인할 수 있습니다.")
                for index in range(4):
                    option = visual_options[index] if index < len(visual_options) else VisualOption(number=index + 1)
                    st.markdown(f"**시각 선택지 {index + 1}**")
                    description = st.text_input("설명", option.description, key=f"visual-desc-{selected_id}-{index}")
                    prompt_option = option.model_copy(update={"description": description})
                    image_prompt = st.text_area(
                        "그림·그래프 생성 프롬프트",
                        suggest_visual_prompt(prompt_option, profile.visual_kind),
                        height=90,
                        key=f"visual-prompt-{selected_id}-{index}",
                    )
                    asset_path = option.asset_path
                    chart_spec = option.chart_spec
                    if profile.visual_kind == "chart" and chart_spec:
                        try:
                            chart_bytes = render_chart(chart_spec)
                            st.image(chart_bytes, width=390)
                            chart_path = asset_dir / f"choice-{index + 1}.png"
                            chart_path.parent.mkdir(parents=True, exist_ok=True)
                            chart_path.write_bytes(chart_bytes)
                            asset_path = str(chart_path.relative_to(ROOT))
                        except Exception as exc:
                            st.error(str(exc))
                    if profile.visual_kind == "scene":
                        upload = st.file_uploader("PNG/JPEG 업로드", type=["png", "jpg", "jpeg"], key=f"visual-upload-{selected_id}-{index}")
                        if upload:
                            try:
                                saved = save_uploaded_asset(upload.getvalue(), f"choice-{index + 1}{Path(upload.name).suffix}", asset_dir)
                                asset_path = str(saved.relative_to(ROOT))
                            except Exception as exc:
                                st.error(str(exc))
                    if asset_path and (ROOT / asset_path).exists():
                        st.image(str(ROOT / asset_path), width=390)
                    visual_options[index:index + 1] = [option.model_copy(update={"description": description, "image_prompt": image_prompt, "asset_path": asset_path})]
            answer = st.selectbox("정답", [1, 2, 3, 4], index=question.answer - 1, key=f"{generated_widget_prefix}-answer")
            explanation = st.text_area("해설", question.explanation, height=110, key=f"{generated_widget_prefix}-explanation")
            target_skill = st.text_input("출제 포인트", question.target_skill, key=f"{generated_widget_prefix}-target-skill")
        with right:
            st.subheader("자동 검사")
            candidate = question.model_copy(update={"dialogue_turns": parse_dialogue(dialogue), "question_prompt": prompt, "choices": choices if profile.visual_kind == "none" else [], "visual_options": visual_options if profile.visual_kind != "none" else [], "answer": answer, "explanation": explanation, "target_skill": target_skill})
            issues = validate_listening_question(candidate, storage.list_examples(approved_only=True))
            if issues:
                for issue in issues:
                    (st.error if issue.severity == "error" else st.warning)(issue.message)
            else:
                st.success("자동 검사 통과")
            current_review = Review.model_validate(item["review"])
            naturalness = st.slider("대본 자연스러움", 1, 5, current_review.naturalness, key=f"{generated_widget_prefix}-naturalness")
            difficulty_fit = st.slider("난이도 적합성", 1, 5, current_review.difficulty_fit, key=f"{generated_widget_prefix}-difficulty-fit")
            distractor_quality = st.slider("오답 품질", 1, 5, current_review.distractor_quality, key=f"{generated_widget_prefix}-distractor-quality")
            topik_fit = st.slider("TOPIK 적합성", 1, 5, current_review.topik_fit, key=f"{generated_widget_prefix}-topik-fit")
            notes = st.text_area("검수 메모", current_review.notes, key=f"{generated_widget_prefix}-notes")
            approved = st.checkbox("최종 승인", current_review.approved, key=f"{generated_widget_prefix}-approved")
            if st.button("수정·평가 저장하고 다음 문항", type="primary"):
                if approved and any(issue.severity == "error" for issue in issues):
                    st.error("오류를 해결한 뒤 승인하세요.")
                else:
                    storage.save_generated_edit(selected_id, candidate.model_dump(mode="json"))
                    storage.save_validation(selected_id, [issue.model_dump() for issue in issues])
                    storage.save_review(selected_id, Review(naturalness=naturalness, difficulty_fit=difficulty_fit, distractor_quality=distractor_quality, topik_fit=topik_fit, notes=notes, approved=approved))
                    next_id = next_sequence_item(item_ids, selected_id)
                    if next_id is not None:
                        st.session_state[
                            f"pending-listening-generated-review-selection-{type_id}"
                        ] = next_id
                        st.session_state["listening_generated_review_message"] = (
                            "대본과 수정·평가 내용을 저장했습니다. 다음 생성 문항으로 이동했습니다."
                        )
                    else:
                        st.session_state["listening_generated_review_message"] = (
                            "대본과 수정·평가 내용을 저장했습니다. 마지막 생성 문항입니다."
                        )
                    st.rerun()


else:
    st.title("내보내기")
    items = storage.list_generated()
    approved_count = sum(item.get("review", {}).get("approved") for item in items)
    st.metric("승인 생성 문항", approved_count)
    txt = to_txt(items)
    json_text = to_json(items)
    csv_text = to_csv(items)
    downloads = st.columns(4)
    downloads[0].download_button("TXT", txt, f"{type_id}.txt", disabled=not approved_count, use_container_width=True)
    downloads[1].download_button("JSON", json_text, f"{type_id}.json", disabled=not approved_count, use_container_width=True)
    downloads[2].download_button("CSV", csv_text, f"{type_id}.csv", disabled=not approved_count, use_container_width=True)
    downloads[3].download_button("자산 ZIP", to_zip(items, ROOT), f"{type_id}_bundle.zip", disabled=not approved_count, use_container_width=True)

    identities = sorted({(item["provider"], item["model"]) for item in items})
    st.subheader("원본 · 모델 A · 모델 B 비교 PDF")
    if len(identities) < 2:
        st.info("서로 다른 모델의 생성 결과가 두 개 이상 필요합니다.")
    else:
        col_a, col_b = st.columns(2)
        model_a = col_a.selectbox("모델 A", identities, format_func=lambda value: f"{provider_label(value[0])} · {value[1]}")
        model_b_options = [value for value in identities if value != model_a]
        model_b = col_b.selectbox("모델 B", model_b_options, format_func=lambda value: f"{provider_label(value[0])} · {value[1]}")
        try:
            pdf_bytes = build_listening_comparison_pdf(storage.list_examples(), items, model_a, model_b, f"TOPIK II 듣기 · {profile.number_range}번 {profile.label}")
            st.download_button("비교 PDF 다운로드", pdf_bytes, f"{type_id}_comparison.pdf", "application/pdf")
        except ValueError as exc:
            st.info(str(exc))
