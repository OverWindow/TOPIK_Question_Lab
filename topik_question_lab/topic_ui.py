from __future__ import annotations

import json
from collections import Counter
from typing import Callable

import streamlit as st

from .topic_bank import (
    InsufficientTopicsError,
    SlotPoolConfigurationError,
    TopicBrief,
    TopicStore,
    generation_units,
)


def _state_key(key_prefix: str) -> str:
    return f"topic-planner:{key_prefix}"


def invalidate_topic_planner(key_prefix: str) -> None:
    st.session_state.pop(_state_key(key_prefix), None)


def _build_plans(
    store: TopicStore,
    *,
    section: str,
    question_type: str,
    providers: list[str],
    units: list[tuple[str, list[int]]],
) -> dict[str, list[TopicBrief]]:
    plans: dict[str, list[TopicBrief]] = {}
    reserved_pairs: set[tuple[str, str]] = set()
    reserved_pair_counts: Counter[tuple[str, str]] = Counter()
    for provider in providers:
        plan = store.select_plan(
            section=section,
            question_type=question_type,
            units=units,
            forbidden_pairs=reserved_pairs,
            initial_pair_counts=reserved_pair_counts,
        )
        plans[provider] = plan
        reserved_pairs.update(brief.pair for brief in plan)
        reserved_pair_counts.update(brief.pair for brief in plan)
    return plans


def planner_rows(
    plans: dict[str, list[TopicBrief]],
    providers: list[str],
    units: list[tuple[str, list[int]]],
    provider_label: Callable[[str], str] = str,
) -> tuple[
    list[dict[str, str]],
    list[tuple[str, int | None, str, tuple[str, list[int]]]],
]:
    """Build rows for every unit, including slots without an assigned topic."""
    rows: list[dict[str, str]] = []
    selectors: list[tuple[str, int | None, str, tuple[str, list[int]]]] = []
    for provider in providers:
        plan = plans.get(provider, [])
        plan_indexes = {brief.unit_key: index for index, brief in enumerate(plan)}
        for unit_key, unit_slots in units:
            index = plan_indexes.get(unit_key)
            brief = plan[index] if index is not None else None
            slots = ", ".join(str(slot) for slot in unit_slots)
            title = brief.title if brief is not None else "소재 미지정"
            label = f"{provider_label(provider)} · {unit_key} · {title}"
            selectors.append((provider, index, label, (unit_key, unit_slots)))
            rows.append(
                {
                    "모델": provider_label(provider),
                    "생성 단위": unit_key,
                    "문항 슬롯": slots,
                    "출처": (
                        "번호별 지정 풀"
                        if brief is not None and brief.source == "slot_pool"
                        else "공용 소재"
                        if brief is not None
                        else "기존 방식"
                    ),
                    "분야": brief.domain if brief is not None else "-",
                    "소재": title,
                    "접근 관점·지침": (
                        brief.prompt_instruction or brief.angle
                        if brief is not None
                        else "문항별 지정 풀이 없어 소재를 강제하지 않습니다."
                    ),
                }
            )
    return rows, selectors


def render_topic_planner(
    store: TopicStore,
    *,
    section: str,
    question_type: str,
    providers: list[str],
    question_numbers: tuple[int, ...],
    count: int,
    shared: bool,
    key_prefix: str,
    provider_label: Callable[[str], str] = str,
) -> tuple[dict[str, list[TopicBrief]], str]:
    """Render and persist a provider-specific plan in Streamlit session state."""
    providers = list(dict.fromkeys(providers))
    units = generation_units(question_numbers, count, shared)
    signature = json.dumps(
        {
            "section": section,
            "question_type": question_type,
            "providers": providers,
            "units": units,
            "topic_revision": store.selection_revision(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    state_key = _state_key(key_prefix)
    state = st.session_state.get(state_key)
    if not isinstance(state, dict) or state.get("signature") != signature:
        try:
            plans = _build_plans(
                store,
                section=section,
                question_type=question_type,
                providers=providers,
                units=units,
            )
            state = {
                "signature": signature,
                "plans": {
                    provider: [brief.model_dump(mode="json") for brief in plan]
                    for provider, plan in plans.items()
                },
                "error": "",
            }
        except (InsufficientTopicsError, SlotPoolConfigurationError) as exc:
            state = {"signature": signature, "plans": {}, "error": str(exc)}
        st.session_state[state_key] = state

    error = str(state.get("error", ""))
    plans = {
        provider: [TopicBrief.model_validate(raw) for raw in raw_plan]
        for provider, raw_plan in state.get("plans", {}).items()
    }

    st.subheader("이번 실행 소재 배정")
    st.caption(
        "번호별 지정 풀이 있으면 그 풀을 우선해 균등 순환하고, 나머지는 최근 50개 조합을 제외한 "
        "공용 소재 은행에서 모델별로 다르게 배정합니다."
    )
    if error:
        st.error(error)
        if st.button("소재 배정 다시 시도", key=f"{key_prefix}-retry-topics"):
            invalidate_topic_planner(key_prefix)
            st.rerun()
        return {}, error

    rows, selectors = planner_rows(plans, providers, units, provider_label)
    st.dataframe(rows, use_container_width=True, hide_index=True)

    if not selectors:
        st.info("현재 생성 수에 포함되는 문항에는 배정할 소재가 없습니다.")
        return plans, ""

    left, middle, right = st.columns([3, 1, 1])
    selected = left.selectbox(
        "개별 재추첨 대상",
        list(range(len(selectors))),
        format_func=lambda value: selectors[value][2],
        key=f"{key_prefix}-topic-row",
        label_visibility="collapsed",
    )
    if middle.button("선택 재추첨", key=f"{key_prefix}-reroll-one", use_container_width=True):
        target_provider, target_index, _, target_unit = selectors[selected]
        if target_index is None:
            unit_key, unit_slots = target_unit
            slots = ", ".join(str(slot) for slot in unit_slots)
            st.error(
                f"{unit_key}({slots}번)는 문항별 지정 풀이 없습니다. "
                "‘소재 은행 > 문항별 지정 풀’에서 이 번호의 풀을 먼저 추가하세요."
            )
            return plans, ""
        current_plan = plans[target_provider]
        target = current_plan[target_index]
        other_briefs = [
            brief
            for provider, plan in plans.items()
            for index, brief in enumerate(plan)
            if not (provider == target_provider and index == target_index)
        ]
        same_provider_others = [
            brief for index, brief in enumerate(current_plan) if index != target_index
        ]
        try:
            other_pair_counts = Counter(brief.pair for brief in other_briefs)
            replacement = store.select_plan(
                section=section,
                question_type=question_type,
                units=[(target.unit_key, target.slot_numbers)],
                forbidden_pairs=set(other_pair_counts),
                initial_pair_counts=other_pair_counts,
                forbidden_topic_ids={target.topic_id, *(brief.topic_id for brief in same_provider_others)},
                initial_domain_counts=Counter(brief.domain for brief in same_provider_others),
            )[0]
            current_plan[target_index] = replacement
            state["plans"][target_provider] = [brief.model_dump(mode="json") for brief in current_plan]
            st.session_state[state_key] = state
            st.rerun()
        except (InsufficientTopicsError, SlotPoolConfigurationError) as exc:
            st.error(str(exc))
    if right.button("전체 재추첨", key=f"{key_prefix}-reroll-all", use_container_width=True):
        invalidate_topic_planner(key_prefix)
        st.rerun()
    return plans, ""
