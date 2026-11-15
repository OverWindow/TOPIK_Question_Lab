"""Keep a random writing topic assignment stable while editing a generation request."""
import streamlit as st

from topik_question_lab.common.generation.topic_bank import TopicBrief, TopicStore
from topik_question_lab.common.generation.writing_topic_catalog import split_writing_topic
from topik_question_lab.menus.writing.writing_pipeline import assign_writing_topics


STATE_KEY = "writing-topic-assignment"


def invalidate_topic_assignment() -> None:
    st.session_state.pop(STATE_KEY, None)


def render_topic_selection(store: TopicStore, slots: list[int]) -> list[TopicBrief] | None:
    st.subheader("이번 생성 소재")
    st.caption("51·52번은 중급, 53·54번은 고급에서 자동 배정합니다. "
               "덜 사용한 소재를 우선해 무작위로 뽑습니다.")
    if st.button("소재 다시 배정", key="writing-reroll-topics"):
        invalidate_topic_assignment()
    signature = (str(store.path.resolve()), tuple(slots),
                 tuple(pool.model_dump_json() for pool in store.list_slot_pools(section="writing", active_only=True)))
    state = st.session_state.get(STATE_KEY)
    if not isinstance(state, dict) or state.get("signature") != signature:
        try:
            plan = assign_writing_topics(store, slots)
        except ValueError as exc:
            invalidate_topic_assignment()
            st.error(str(exc))
            return None
        state = {"signature": signature, "plan": [brief.model_dump() for brief in plan]}
        st.session_state[STATE_KEY] = state
    plan = [TopicBrief.model_validate(item) for item in state["plan"]]
    st.dataframe([
        {"순서": position, "문항": brief.slot_numbers[0],
         **dict(zip(("수준", "분야", "소재"), split_writing_topic(brief.title)))}
        for position, brief in enumerate(plan, 1)
    ], hide_index=True, width="stretch")
    return plan
