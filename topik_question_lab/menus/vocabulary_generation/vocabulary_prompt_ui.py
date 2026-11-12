"""Editable instructions and exact request previews for word generation."""
import streamlit as st

from topik_question_lab.common.vocabulary.vocabulary_models import source_snapshot, digest
from topik_question_lab.menus.vocabulary_generation.vocabulary_prompts import PROMPT_FIELDS, PROMPT_VERSION, prompt_config, build_prompt


def _reset_prompts(draft_key, widget_prefix):
    defaults = prompt_config()
    st.session_state[draft_key] = {key: defaults[key] for key in PROMPT_FIELDS}
    st.session_state[f"{draft_key}-version"] = PROMPT_VERSION
    for key in PROMPT_FIELDS:
        st.session_state[f"{widget_prefix}-{key}"] = defaults[key]


def _remember_prompt(draft_key, widget_key, field):
    st.session_state[draft_key][field] = st.session_state[widget_key]


def render_prompt_editor(database_key, selected):
    draft_key = f"vocab-prompt-draft-{database_key}"
    widget_prefix = f"vocab-prompt-{database_key}"
    if draft_key not in st.session_state:
        defaults = prompt_config()
        st.session_state[draft_key] = {key: defaults[key] for key in PROMPT_FIELDS}
        st.session_state[f"{draft_key}-version"] = PROMPT_VERSION
    for key, value in prompt_config().items():
        if key in PROMPT_FIELDS:
            st.session_state[draft_key].setdefault(key, value)
    st.subheader("2. AI 프롬프트 확인")
    st.caption("기본 지침으로 바로 실행할 수 있습니다. 필요하면 아래에서 수정하세요.")
    if st.session_state.get(f"{draft_key}-version") != PROMPT_VERSION:
        st.info("이전 프롬프트 초안을 유지하고 있습니다. 새 검수 기준을 사용하려면 아래 ‘AI 프롬프트 보기·수정’에서 "
                "‘프롬프트 기본값 복원’을 누르세요.")
    with st.expander("AI 프롬프트 보기·수정"):
        st.caption("공통 지침은 모든 호출에, 검수 지침은 1차·최종 검수에, 수정 생성 지침은 1차 검수 실패 시 전달됩니다.")
        st.caption("편집 내용은 새 생성 작업을 시작할 때 작업 이력에 저장됩니다. 화면 이동 중에는 초안을 유지합니다.")
        st.caption("단어 정보와 출력 형식은 자동으로 붙습니다. 문항 수·보기·허용 답안 검증 기준은 유지됩니다.")
        st.button("프롬프트 기본값 복원", key=f"{widget_prefix}-reset", on_click=_reset_prompts,
                  args=(draft_key, widget_prefix))
        tabs = st.tabs([*PROMPT_FIELDS.values(), "최종 전달 내용"])
        for tab, (field, label) in zip(tabs, PROMPT_FIELDS.items()):
            key = f"{widget_prefix}-{field}"
            if key not in st.session_state:
                st.session_state[key] = st.session_state[draft_key][field]
            with tab:
                st.text_area(label, height=330, key=key, on_change=_remember_prompt,
                             args=(draft_key, key, field))
        try:
            config = prompt_config(st.session_state[draft_key])
        except ValueError as exc:
            st.error(str(exc))
            return None
        with tabs[-1]:
            if selected is None:
                st.info("단어를 선택하면 실제 생성 요청에 전달할 내용을 확인할 수 있습니다.")
            else:
                user = build_prompt("generate", source_snapshot(selected), {}, config)
                key = digest((config["system"], user))[:16]
                st.caption("선택 단어의 생성 요청 미리보기입니다. 두 번째 검수 요청에는 생성된 문제 묶음이 추가됩니다.")
                st.text_area("최종 System 프롬프트", config["system"], height=240, disabled=True, key=f"vocab-system-preview-{key}")
                st.text_area("최종 User 프롬프트", user, height=320, disabled=True, key=f"vocab-user-preview-{key}")
    return config


def render_prompt_history(task):
    calls = task["checkpoint"].get("calls", [])
    if not calls:
        return
    with st.expander("실제 AI 전달 프롬프트 이력"):
        labels = {"generate": "최초 생성", "review": "1차 검수", "repair": "지적 문항 수정", "final_review": "최종 검수"}
        selected = st.selectbox("모델 요청", list(range(len(calls))),
            format_func=lambda i: f"{i + 1}. {labels.get(calls[i].get('phase', calls[i]['stage']), calls[i]['stage'])}",
            key=f"vocab-call-{task['generation_id']}")
        call = calls[selected]
        key = f"{task['generation_id']}-{selected}"
        if "system" in call:
            st.text_area("전달한 System 프롬프트", call["system"], height=240, disabled=True, key=f"vocab-history-system-{key}")
        else:
            st.caption("이전 기록에는 System 프롬프트가 별도로 저장되지 않았습니다.")
        st.text_area("전달한 User 프롬프트", call.get("prompt", ""), height=320, disabled=True,
                     key=f"vocab-history-user-{key}")
