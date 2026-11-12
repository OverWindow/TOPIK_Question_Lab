"""Display a question consistently across registration and editing."""
import streamlit as st


def show_question(question):
    seen = set()
    for field in ("auxiliary_text", "passage", "stem", "question_prompt"):
        text = question.get(field)
        if text and text not in seen:
            st.write(text)
            seen.add(text)
    for turn in question.get("dialogue_turns") or []:
        st.write(f"{turn.get('speaker', '')}: {turn.get('text', '')}")
    for n, choice in enumerate(question.get("choices") or [], 1):
        st.write(f"{n}. {choice}")
    for option in question.get("visual_options") or []:
        st.write(f"{option.get('number')}. {option.get('description', '')}")
    st.write(f"정답: {question.get('answer', '미기록')}")
    st.caption(f"해설: {question.get('explanation') or '미기록'}")
