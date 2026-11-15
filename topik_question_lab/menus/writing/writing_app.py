from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError
import streamlit as st

from topik_question_lab.common.generation.providers import can_gateway_call, manual_result
from topik_question_lab.common.paths import application_root
from topik_question_lab.common.generation.topic_bank import TOPIC_STORE_SCHEMA_VERSION, TopicStore
from topik_question_lab.common.writing_models import PROFILES, WritingQuestion
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing.writing_pipeline import (
    PDF_ROOT, build_prompts, export_json, export_text, generate_writing, load_references,
    save_result, validation_message,
)
from topik_question_lab.menus.writing.writing_ui import edit_question, pdf_page, render_question, render_image_actions
from topik_question_lab.menus.writing.writing_images import generate_result_images, image_path
from topik_question_lab.menus.writing.writing_topic_ui import invalidate_topic_assignment, render_topic_selection
from topik_question_lab.menus.writing.writing_publication_ui import render_publication
from topik_question_lab.menus.writing.writing_prompt_ui import (
    render_analysis, render_prompt_workshop, render_provider, select_references,
)
from topik_question_lab.menus.writing.writing_prompts import prompt_settings


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="TOPIK II 쓰기 PBT", page_icon="✍️", layout="wide")
st.title("TOPIK II 쓰기 PBT")
st.caption("유형 선택 → 기출 확인·유형 분석 → 프롬프트 조정 → 한 문항 생성 → 수정·내보내기 → Hub 세트 저장")
st.caption("문제 생성의 자동 검사는 문항 형식을 확인합니다. 답안 평가는 ‘TOPIK 쓰기 → 평가 프롬프트 실험실’에서 진행합니다.")


# Refresh storage cached before selection-based publication was loaded.
@st.cache_resource(validate=lambda store: callable(getattr(store, "selection_source_key", None)))
def get_storage(root: str) -> WritingStorage:
    return WritingStorage(Path(root) / "data/writing_pbt.db")


@st.cache_resource
def get_topic_store(root: str, schema_version: int) -> TopicStore:
    return TopicStore(Path(root) / "data/topic_bank.db")


storage = get_storage(str(ROOT))
references = load_references()
slot = st.sidebar.selectbox("쓰기 유형", list(PROFILES), format_func=lambda value: PROFILES[value].label, key="writing-type")
stages = ["1. 기출 문항", "2. 유형 분석", "3. 프롬프트 작업실", "4. 문제 생성", "5. 수정·내보내기", "6. Hub 세트 저장"]
if st.session_state.get("writing-stage") not in stages:
    st.session_state["writing-stage"] = stages[0]
stage = st.sidebar.radio("쓰기 작업 단계", stages, key="writing-stage")
st.sidebar.caption("51·52번 각 10점 / 53번 30점 / 54번 50점")

if stage == "1. 기출 문항":
    st.subheader(f"{slot}번 기출 문항")
    st.caption("문제지 원문을 전사하고 빈칸·줄바꿈을 정리했습니다. 도표의 단위는 명시한 단위로 환산했으며, "
               "절대 수치가 없는 배수·순위 그래프는 표로 옮겼습니다. 원본 PDF로 원래 배치를 확인할 수 있습니다.")
    available = sorted([r for r in references if r.question.type_slot == slot], key=lambda r: r.source_exam, reverse=True)
    selected_key = st.selectbox("기출 회차", [r.source_key for r in available],
                                format_func=lambda value: f"{value.split(':')[0]}회", key=f"writing-reference-exam-{slot}")
    reference = next(r for r in available if r.source_key == selected_key)
    render_question(reference.question, instruction=reference.source_instruction)
    with st.expander("새 문항에 적용할 표준 발문"):
        st.write(reference.question.instruction)
        st.caption("출처: 쓰기 발문 안내.pdf")
    path = PDF_ROOT / reference.source_file
    if path.is_file():
        st.download_button("기출 문제지 PDF 받기", path.read_bytes(), path.name, "application/pdf")
        if st.checkbox("원본 페이지 보기", key="writing-show-pdf"):
            try:
                st.image(pdf_page(str(path), path.stat().st_mtime_ns, reference.source_page), caption=f"{path.name} · {reference.source_page}쪽")
            except (OSError, RuntimeError):
                st.warning("원본 미리보기를 열 수 없습니다. PDF를 내려받아 확인하세요.")
    else:
        st.info("원본 PDF가 없어 구조화된 기출만 표시합니다.")

elif stage == "2. 유형 분석":
    render_analysis(storage, references, slot)

elif stage == "3. 프롬프트 작업실":
    render_prompt_workshop(storage, references, slot)

elif stage == "4. 문제 생성":
    st.subheader(f"{PROFILES[slot].label} · 한 문항 생성")
    st.caption("유형 분석서와 프롬프트 작업실에 저장한 설정을 적용합니다. 이번 문항의 추가 요청을 조정한 뒤 생성하세요.")
    if st.session_state.pop(f"writing-reset-manual-{slot}", False):
        st.session_state[f"writing-manual-json-{slot}"] = ""
    slots = [slot]
    topic_store = get_topic_store(str(ROOT), TOPIC_STORE_SCHEMA_VERSION)
    topic_plan = render_topic_selection(topic_store, slots)
    if topic_plan is None:
        st.stop()
    topic = st.text_area("이번 문항 추가 요청", placeholder="예: 배정 소재를 대학 생활과 관련된 상황으로 구체화해 주세요.", key=f"writing-topic-{slot}")
    selected_references = select_references(references, slot, prefix="writing-generation")
    system, user = build_prompts(slots, topic, selected_references, topic_plan, settings=prompt_settings(storage, slot))
    with st.expander("생성 프롬프트 확인"):
        st.text_area("시스템 프롬프트", system, height=240, disabled=True)
        st.text_area("문항 생성 요청", user, height=320, disabled=True)
    provider, model, capabilities = render_provider(storage, use_saved_default=True)
    create_image = slot == 53 and st.checkbox("자료 이미지도 함께 생성", value=True, key="writing-create-image")
    if slot == 53:
        st.caption("이미지는 ‘설정’에 저장된 이미지 서비스·모델과 53번 디자인 지침으로 생성합니다.")
    outcome_key = f"writing-last-outcome-{slot}"
    if st.button("한 문항 생성", type="primary", disabled=not model or not can_gateway_call(provider), key="writing-generate"):
        with st.spinner("쓰기 문항 생성 중…"):
            st.session_state[outcome_key] = generate_writing(
                storage, provider=provider, model=model, system=system, user=user, slots=slots,
                model_capabilities=capabilities, topic_plan=topic_plan, topic_store=topic_store)
            if not st.session_state[outcome_key]["error"]:
                if create_image:
                    with st.spinner("기출 스타일의 자료 이미지 생성 중…"):
                        st.session_state[outcome_key] = generate_result_images(storage, st.session_state[outcome_key])
                invalidate_topic_assignment()
                st.rerun()
    with st.expander("외부 모델 응답 붙여넣기"):
        raw = st.text_area("생성 응답 JSON", height=230, key=f"writing-manual-json-{slot}")
        if st.button("응답 검사·저장", disabled=not raw.strip(), key="writing-import"):
            result = manual_result(provider, model or "manual", "generation", raw)
            st.session_state[outcome_key] = save_result(
                storage, result, system, user, slots, topic_plan=topic_plan, topic_store=topic_store)
            if not st.session_state[outcome_key]["error"]:
                if create_image:
                    with st.spinner("기출 스타일의 자료 이미지 생성 중…"):
                        st.session_state[outcome_key] = generate_result_images(storage, st.session_state[outcome_key])
                invalidate_topic_assignment()
                st.session_state[f"writing-reset-manual-{slot}"] = True
                st.rerun()
    outcome = st.session_state.get(outcome_key)
    if outcome:
        if outcome["error"]:
            st.error(f"문항을 저장하지 않았습니다: {outcome['error']}")
        else:
            st.success(f"{len(outcome['question_ids'])}문항 저장 완료 · 실행 #{outcome['run_id']}. ‘5. 수정·내보내기’에서 확인하세요.")
            saved = storage.list_generated(outcome["run_id"])
            if saved:
                question = WritingQuestion.model_validate(saved[0]["question"])
                render_question(question)
                if outcome.get("image_error") and not image_path(question).is_file():
                    st.warning("문항은 저장했습니다. " + outcome["image_error"])
                render_image_actions(question, storage, key=f"writing-result-image-{saved[0]['id']}")
            if outcome.get("warning"):
                st.warning(outcome["warning"])
    with st.expander("최근 생성 이력"):
        runs = storage.list_runs("generation")[:20]
        if runs:
            run_id = st.selectbox("실행 기록", [row["id"] for row in runs], key="writing-run")
            run = next(row for row in runs if row["id"] == run_id)
            result = json.loads(run["result_json"])
            st.caption(f"{run['created_at']} · {run['model']} · {result.get('duration_seconds', 0):.1f}초")
            if result.get("error"):
                st.warning(result["error"])
            if plan := result.get("request_parameters", {}).get("writing_topic_plan"):
                st.dataframe([{"순서": b["unit_key"].removeprefix("item-"), "문항": b["slot_numbers"][0],
                               "선택 소재": b["title"]} for b in plan], hide_index=True, width="stretch")
            st.text_area("원본 응답", result.get("raw_response", ""), height=200, disabled=True, key=f"writing-raw-{run_id}")
        else:
            st.info("아직 생성 이력이 없습니다.")

elif stage == "5. 수정·내보내기":
    records = storage.list_generated()
    if not records:
        st.info("저장된 쓰기 문항이 없습니다. ‘4. 문제 생성’에서 문항을 만들어 주세요.")
        st.stop()
    slot_filter = st.selectbox("문항 유형", [None, 51, 52, 53, 54],
                              format_func=lambda value: "전체" if value is None else PROFILES[value].label, key="writing-filter")
    records = [row for row in records if slot_filter is None or row["question"]["type_slot"] == slot_filter]
    if not records:
        st.info("선택한 유형의 저장 문항이 없습니다.")
        st.stop()
    by_id = {row["id"]: row for row in records}
    label = lambda value: f"#{value} · {by_id[value]['question']['type_slot']}번 · {by_id[value]['question']['topic']} · 실행 #{by_id[value]['run_id']}"
    selected_id = st.selectbox("수정할 문항", list(by_id), format_func=label, key=f"writing-selected-{slot_filter}")
    question = WritingQuestion.model_validate(by_id[selected_id]["question"])
    render_question(question)
    if "writing-saved" in st.session_state:
        st.success(st.session_state.pop("writing-saved"))
    with st.expander("문항 수정", expanded=True):
        draft = edit_question(question, selected_id)
        if st.button("수정 내용 저장", key="writing-save"):
            try:
                storage.save_generated_edit(selected_id, draft)
            except (ValidationError, ValueError) as exc:
                st.error(validation_message(exc) if isinstance(exc, ValidationError) else str(exc))
            else:
                st.session_state["writing-saved"] = "수정 내용을 저장했습니다. 아래 내보내기에도 반영됩니다."
                st.rerun()
    render_image_actions(question, storage, key=f"writing-edit-image-{selected_id}")
    st.subheader("내보내기")
    export_ids = st.multiselect("내보낼 문항", list(by_id), default=[selected_id], format_func=label,
                                key=f"writing-export-{slot_filter}-{selected_id}")
    selected = sorted((by_id[value] for value in export_ids), key=lambda row: (row["run_id"], row["position"]))
    questions = [WritingQuestion.model_validate(row["question"]) for row in selected]
    st.caption("저장된 수정본을 실행·문항 순서로 내보냅니다.")
    col1, col2 = st.columns(2)
    col1.download_button("JSON 받기", export_json(questions), "topik_writing_pbt.json", "application/json", disabled=not questions)
    col2.download_button("TXT 받기", export_text(questions), "topik_writing_pbt.txt", "text/plain", disabled=not questions)

else:
    render_publication(storage, os.getenv("DATABASE_URL", "").strip())
