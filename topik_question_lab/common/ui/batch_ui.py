"""Optional batch filters; legacy questions remain visible by default."""
import streamlit as st


def select_generation_batch(values, key, batch_id_for):
    ids = list(dict.fromkeys(batch_id_for(value) for value in values if batch_id_for(value)))
    if not ids:
        return values
    choices = ["all", "legacy", *ids]
    selected = st.selectbox(
        "생성 묶음 필터", choices, key=key,
        format_func=lambda value: "전체" if value == "all" else "기존 유형별 생성" if value == "legacy" else f"일괄 생성 · {value[:8]}",
    )
    if selected == "all":
        return values
    return [value for value in values if (batch_id_for(value) or "legacy") == selected]


def filter_generation_batch(items, section, type_id):
    return select_generation_batch(items, f"review-batch-{section}-{type_id}", lambda item: item.get("batch_id"))
