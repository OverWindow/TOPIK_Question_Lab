from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import streamlit as st

from topik_question_lab.listening_profiles import LISTENING_TYPE_PROFILES
from topik_question_lab.prompt_profiles import QUESTION_TYPE_PROFILES
from topik_question_lab.topic_bank import (
    TOPIC_STORE_SCHEMA_VERSION,
    SlotTopicPool,
    TopicDefinition,
    TopicStore,
    format_slot_numbers,
    parse_pool_items,
    parse_slot_numbers,
)


ROOT = Path(__file__).resolve().parents[1]
TOPIC_DB_PATH = ROOT / "data" / "topic_bank.db"


@st.cache_resource
def get_topic_store(schema_version: int) -> TopicStore:
    del schema_version  # The argument versions Streamlit's resource cache.
    return TopicStore(TOPIC_DB_PATH)


def reading_label(type_id: str) -> str:
    profile = QUESTION_TYPE_PROFILES[type_id]
    return f"{profile.number_range} · {profile.label}"


def listening_label(type_id: str) -> str:
    profile = LISTENING_TYPE_PROFILES[type_id]
    return f"{profile.number_range}번 · {profile.label}"


def render_global_bank(store: TopicStore) -> None:
    all_topics = store.list_topics()
    domains = sorted({topic.domain for topic in all_topics})
    usages = store.list_usages(5000)
    metrics = st.columns(4)
    metrics[0].metric("전체 소재", len(all_topics))
    metrics[1].metric("활성 소재", sum(topic.active for topic in all_topics))
    metrics[2].metric("분야", len(domains))
    metrics[3].metric("신규 생성 사용 기록", len(usages))

    filter_col1, filter_col2, filter_col3 = st.columns([2, 1, 1])
    query = filter_col1.text_input("검색", placeholder="소재명, 분야 또는 접근 관점", key="global-query")
    domain_filter = filter_col2.selectbox("분야", ["전체", *domains], key="global-domain")
    active_filter = filter_col3.selectbox("상태", ["전체", "활성", "비활성"], key="global-status")
    filtered = store.list_topics(
        active_only=active_filter == "활성",
        query=query,
        domain="" if domain_filter == "전체" else domain_filter,
    )
    if active_filter == "비활성":
        filtered = [topic for topic in filtered if not topic.active]

    st.dataframe(
        [
            {
                "ID": topic.id,
                "분야": topic.domain,
                "소재": topic.title,
                "접근 관점": " / ".join(topic.angles),
                "읽기 유형": len(topic.reading_types),
                "듣기 유형": len(topic.listening_types),
                "상태": "활성" if topic.active else "비활성",
                "구분": "기본" if topic.is_default else "사용자",
            }
            for topic in filtered
        ],
        use_container_width=True,
        hide_index=True,
    )

    if filtered:
        selected_id = st.selectbox(
            "수정할 소재",
            [topic.id for topic in filtered],
            format_func=lambda value: next(
                f"{topic.domain} · {topic.title}" for topic in filtered if topic.id == value
            ),
            key="global-selected",
        )
        selected = next(topic for topic in filtered if topic.id == selected_id)
        with st.form(f"edit-topic-{selected.id}"):
            col1, col2 = st.columns(2)
            domain = col1.text_input("분야", selected.domain)
            title = col2.text_input("소재명", selected.title)
            angles = [
                st.text_input(f"접근 관점 {index}", value)
                for index, value in enumerate(selected.angles, start=1)
            ]
            reading_types = st.multiselect(
                "적용할 읽기 유형",
                list(QUESTION_TYPE_PROFILES),
                default=selected.reading_types,
                format_func=reading_label,
            )
            listening_types = st.multiselect(
                "적용할 듣기 유형",
                list(LISTENING_TYPE_PROFILES),
                default=selected.listening_types,
                format_func=listening_label,
            )
            active = st.checkbox("생성 후보로 사용", selected.active)
            if st.form_submit_button("소재 저장", type="primary"):
                try:
                    store.save_topic(
                        TopicDefinition(
                            id=selected.id,
                            domain=domain.strip(),
                            title=title.strip(),
                            angles=[value.strip() for value in angles],
                            reading_types=reading_types,
                            listening_types=listening_types,
                            active=active,
                            is_default=selected.is_default,
                        )
                    )
                    st.success("소재를 저장했습니다.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    with st.expander("새 공용 소재 추가"):
        with st.form("add-topic"):
            col1, col2 = st.columns(2)
            new_domain = col1.text_input("새 소재 분야")
            new_title = col2.text_input("새 소재명")
            new_angles = [st.text_input(f"새 접근 관점 {index}") for index in range(1, 4)]
            new_reading_types = st.multiselect(
                "읽기 유형", list(QUESTION_TYPE_PROFILES), default=list(QUESTION_TYPE_PROFILES),
                format_func=reading_label, key="new-topic-reading-types",
            )
            new_listening_types = st.multiselect(
                "듣기 유형", list(LISTENING_TYPE_PROFILES), default=list(LISTENING_TYPE_PROFILES),
                format_func=listening_label, key="new-topic-listening-types",
            )
            if st.form_submit_button("새 소재 저장", type="primary"):
                try:
                    store.save_topic(
                        TopicDefinition(
                            id=f"custom_{uuid4().hex}", domain=new_domain.strip(), title=new_title.strip(),
                            angles=[value.strip() for value in new_angles], reading_types=new_reading_types,
                            listening_types=new_listening_types, active=True, is_default=False,
                        )
                    )
                    st.success("새 소재를 추가했습니다.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    st.divider()
    left, right = st.columns([1, 3])
    if left.button("누락된 기본 소재 복원", use_container_width=True):
        inserted = store.seed_defaults()
        st.success(f"누락된 기본 소재 {inserted}개를 복원했습니다. 기존 수정값은 유지했습니다.")
        st.rerun()
    right.caption("소재를 제거하려면 ‘생성 후보로 사용’을 해제하세요. 사용 이력은 보존됩니다.")

    if usages:
        with st.expander("최근 공용 소재 사용 이력"):
            st.dataframe(
                [{"시각": row["created_at"], "영역": "읽기" if row["section"] == "reading" else "듣기",
                  "유형": row["question_type"], "모델": row["provider"], "분야": row["domain"],
                  "소재": row["title"], "접근 관점": row["angle"]} for row in usages[:100]],
                use_container_width=True, hide_index=True,
            )


def _section_label(section: str) -> str:
    return "읽기" if section == "reading" else "듣기"


def render_slot_pools(store: TopicStore) -> None:
    all_pools = store.list_slot_pools()
    usages = store.list_slot_pool_usages(5000)
    metrics = st.columns(4)
    metrics[0].metric("전체 지정 풀", len(all_pools))
    metrics[1].metric("활성 지정 풀", sum(pool.active for pool in all_pools))
    metrics[2].metric("등록 항목", sum(len(pool.items) for pool in all_pools))
    metrics[3].metric("지정 풀 사용 기록", len(usages))

    st.subheader("문항 번호별 소재 적용 현황")
    coverage_col1, coverage_col2 = st.columns(2)
    coverage_section = coverage_col1.selectbox(
        "현황 영역",
        ["reading", "listening"],
        index=1,
        format_func=_section_label,
        key="pool-coverage-section",
    )
    coverage_range = coverage_col2.selectbox(
        "문항 구간",
        ["1~20", "21~40", "41~50", "전체"],
        key="pool-coverage-range",
    )
    range_slots = {
        "1~20": range(1, 21),
        "21~40": range(21, 41),
        "41~50": range(41, 51),
        "전체": range(1, 51),
    }[coverage_range]
    coverage_rows = []
    missing_slots: list[int] = []
    for slot in range_slots:
        matches = [
            pool
            for pool in all_pools
            if pool.section == coverage_section and slot in pool.slot_numbers
        ]
        active = next((pool for pool in matches if pool.active), None)
        if active is not None:
            status = "활성"
            pool_name = active.name
            item_count = len(active.items)
        elif matches:
            status = "비활성"
            pool_name = " / ".join(pool.name for pool in matches)
            item_count = sum(len(pool.items) for pool in matches)
        else:
            status = "미지정(기존 방식)"
            pool_name = "-"
            item_count = 0
            missing_slots.append(slot)
        coverage_rows.append(
            {
                "문항 번호": slot,
                "상태": status,
                "적용 풀": pool_name,
                "항목 수": item_count,
            }
        )
    st.dataframe(coverage_rows, use_container_width=True, hide_index=True, height=420)
    if missing_slots:
        st.caption(
            "미지정 번호는 기존 생성 방식을 사용합니다. 소재를 직접 배정하려면 아래에서 새 풀을 추가하세요. "
            f"현재 구간 미지정: {', '.join(map(str, missing_slots))}번"
        )

    filter_col1, filter_col2 = st.columns(2)
    section_filter = filter_col1.selectbox("영역", ["전체", "읽기", "듣기"], key="pool-section-filter")
    status_filter = filter_col2.selectbox("상태", ["전체", "활성", "비활성"], key="pool-status-filter")
    filtered = [
        pool for pool in all_pools
        if (section_filter == "전체" or _section_label(pool.section) == section_filter)
        and (status_filter == "전체" or ("활성" if pool.active else "비활성") == status_filter)
    ]
    st.dataframe(
        [{"영역": _section_label(pool.section), "적용 번호": format_slot_numbers(pool.slot_numbers),
          "풀 이름": pool.name, "항목 수": len(pool.items), "상태": "활성" if pool.active else "비활성",
          "구분": "기본" if pool.is_default else "사용자", "수정": "사용자 수정" if pool.user_modified else "초기값"}
         for pool in filtered],
        use_container_width=True, hide_index=True,
    )

    if filtered:
        selected_id = st.selectbox(
            "수정할 지정 풀", [pool.id for pool in filtered],
            format_func=lambda value: next(
                f"{_section_label(pool.section)} {format_slot_numbers(pool.slot_numbers)}번 · {pool.name}"
                for pool in filtered if pool.id == value
            ), key="slot-pool-selected",
        )
        selected = next(pool for pool in filtered if pool.id == selected_id)
        with st.form(f"edit-slot-pool-{selected.id}"):
            col1, col2 = st.columns(2)
            section = col1.selectbox(
                "적용 영역", ["reading", "listening"], index=0 if selected.section == "reading" else 1,
                format_func=_section_label,
            )
            slots = col2.text_input("적용 번호", format_slot_numbers(selected.slot_numbers), help="예: 5 또는 11~12, 15")
            name = st.text_input("풀 이름", selected.name)
            instruction = st.text_area("프롬프트 지침", selected.prompt_instruction, height=90)
            items_text = st.text_area(
                "소재 항목", "\n".join(selected.items), height=260,
                help="줄바꿈 또는 쉼표로 구분합니다. 괄호 안 쉼표는 항목의 일부로 유지됩니다.",
            )
            active = st.checkbox("이 지정 풀 사용", selected.active)
            if st.form_submit_button("지정 풀 저장", type="primary"):
                try:
                    store.save_slot_pool(
                        SlotTopicPool(
                            id=selected.id, section=section, slot_numbers=parse_slot_numbers(slots), name=name.strip(),
                            prompt_instruction=instruction.strip(), items=parse_pool_items(items_text), active=active,
                            is_default=selected.is_default, user_modified=True,
                        )
                    )
                    st.success("번호별 지정 풀을 저장했습니다.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    with st.expander("새 번호별 지정 풀 추가"):
        quick_options: list[str | int] = ["직접 입력", *missing_slots]
        quick_slot = st.selectbox(
            "미지정 번호 빠른 선택",
            quick_options,
            format_func=lambda value: value if isinstance(value, str) else f"{value}번",
            key=f"new-pool-quick-slot-{coverage_section}-{coverage_range}",
        )
        with st.form("add-slot-pool"):
            col1, col2 = st.columns(2)
            new_section = col1.selectbox(
                "새 풀 영역",
                ["reading", "listening"],
                index=0 if coverage_section == "reading" else 1,
                format_func=_section_label,
            )
            new_slots = col2.text_input(
                "새 풀 적용 번호",
                value="" if isinstance(quick_slot, str) else str(quick_slot),
                placeholder="예: 13~15",
                key=f"new-pool-slots-{coverage_section}-{coverage_range}-{quick_slot}",
            )
            new_name = st.text_input("새 풀 이름")
            new_instruction = st.text_area("새 풀 프롬프트 지침", height=80)
            new_items = st.text_area(
                "새 풀 소재 항목", height=180, placeholder="항목 하나\n항목 둘\n책(소설, 시집)",
            )
            if st.form_submit_button("새 지정 풀 저장", type="primary"):
                try:
                    store.save_slot_pool(
                        SlotTopicPool(
                            id=f"custom_pool_{uuid4().hex}", section=new_section,
                            slot_numbers=parse_slot_numbers(new_slots), name=new_name.strip(),
                            prompt_instruction=new_instruction.strip(), items=parse_pool_items(new_items),
                            active=True, is_default=False, user_modified=True,
                        )
                    )
                    st.success("새 번호별 지정 풀을 추가했습니다.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    st.divider()
    left, right = st.columns([1, 3])
    if left.button("누락된 기본 지정 풀 복원", use_container_width=True):
        inserted = store.seed_default_slot_pools()
        st.success(f"누락된 기본 지정 풀 {inserted}개를 복원했습니다. 기존 수정값은 유지했습니다.")
        st.rerun()
    right.caption("풀을 끄면 그 번호는 기존 공용 소재 방식으로 돌아갑니다. 이력은 삭제되지 않습니다.")

    if usages:
        with st.expander("최근 번호별 지정 풀 사용 이력"):
            st.dataframe(
                [{"시각": row["created_at"], "영역": _section_label(row["section"]),
                  "유형": row["question_type"], "모델": row["provider"], "풀": row["pool_name"],
                  "선정 항목": row["item"]} for row in usages[:100]],
                use_container_width=True, hide_index=True,
            )


st.set_page_config(page_title="TOPIK 소재 은행", page_icon="🗂️", layout="wide")
st.title("소재 은행")
st.caption("공용 소재와 문항 번호별 사용자 지정 풀을 관리합니다.")

topic_store = get_topic_store(TOPIC_STORE_SCHEMA_VERSION)
global_tab, slot_tab = st.tabs(["공용 소재 은행", "문항별 지정 풀"])
with global_tab:
    render_global_bank(topic_store)
with slot_tab:
    render_slot_pools(topic_store)
