from __future__ import annotations

from uuid import uuid4

import streamlit as st

from topik_question_lab.common.paths import application_root
from topik_question_lab.common.generation.exam_profiles import profiles_for_exam
from topik_question_lab.common.generation.listening_profiles import LISTENING_TYPE_PROFILES
from topik_question_lab.common.generation.prompt_profiles import QUESTION_TYPE_PROFILES
from topik_question_lab.common.generation.topic_bank import (
    TOPIC_STORE_SCHEMA_VERSION, SlotTopicPool, TopicDefinition, TopicStore,
    format_slot_numbers, parse_pool_items, parse_slot_numbers,
)
from topik_question_lab.menus.topic_bank.topic_browser import (
    application_matches, applications_for_exam, pool_exam_slots, query_number,
)


@st.cache_resource
def get_topic_store(schema_version: int) -> TopicStore:
    del schema_version
    return TopicStore(application_root() / "data/topic_bank.db")


def section_label(section: str) -> str:
    return "읽기" if section == "reading" else "듣기"


def type_label(section: str, type_id: str) -> str:
    profiles = QUESTION_TYPE_PROFILES if section == "reading" else LISTENING_TYPE_PROFILES
    profile = profiles[type_id]
    ibt_slots = sorted({
        slot for ibt in profiles_for_exam(section, "ibt").values()
        if ibt.base_type_id == type_id for slot in ibt.question_numbers
    })
    suffix = f" / IBT {format_slot_numbers(ibt_slots)}번" if ibt_slots else ""
    return f"PBT {profile.number_range}번{suffix} · {profile.label}"


def saved(message: str) -> None:
    st.session_state["topic-save-message"] = message
    st.rerun()


def initialize_fields(values: dict) -> None:
    # Stable widget defaults retain edits across saving and table selection.
    for key, value in values.items():
        st.session_state.setdefault(key, value)


def selected_row(rows: list[dict], key: str, *, height: int = 400) -> int:
    event = st.dataframe(
        rows, width="stretch", hide_index=True, height=height,
        on_select="rerun", selection_mode="single-row", key=key,
        column_config={
            "문항": st.column_config.TextColumn(width="small"),
            "유형": st.column_config.TextColumn(width="small"),
            "적용 풀": st.column_config.TextColumn(width="medium"),
            "소재 수": st.column_config.NumberColumn(width="small"),
        },
    )
    selected = event["selection"]["rows"]
    return selected[0] if selected and selected[0] < len(rows) else 0


def render_pool(store: TopicStore, pool: SlotTopicPool) -> None:
    st.subheader(pool.name)
    links = [
        f"{exam.upper()} {format_slot_numbers(pool_exam_slots(pool, exam)) or '연결 없음'}"
        for exam in ("pbt", "ibt")
    ]
    st.caption(f"{section_label(pool.section)} · {' / '.join(links)} · {'사용 중' if pool.active else '사용 안 함'}")
    st.caption("같은 풀을 사용하는 PBT·IBT 문항에 수정한 소재와 지침이 함께 적용됩니다.")
    query = st.text_input("풀 안의 소재 검색", key=f"pool-search-{pool.id}")
    rows = [{"번호": index, "소재": item} for index, item in enumerate(pool.items, 1) if query.casefold() in item.casefold()]
    st.caption(f"전체 {len(pool.items)}개 · 검색 결과 {len(rows)}개")
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True, height=300)
    else:
        st.info("검색 결과가 없습니다.")
    with st.expander("선정 방식·생성 지침"):
        st.write("사용 횟수가 적은 소재를 우선 선택합니다. 같은 실행의 중복은 가능한 한 피하고, 공통 지문·대본은 소재 하나를 공유합니다.")
        st.write(pool.prompt_instruction)
    with st.expander("소재 풀 수정"):
        with st.form(f"edit-pool-{pool.id}"):
            initialize_fields({
                f"pool-name-{pool.id}": pool.name, f"pool-items-{pool.id}": "\n".join(pool.items),
                f"pool-instruction-{pool.id}": pool.prompt_instruction, f"pool-active-{pool.id}": pool.active,
                f"pool-slots-{pool.id}": format_slot_numbers(pool.slot_numbers),
            })
            name = st.text_input("풀 이름", key=f"pool-name-{pool.id}")
            items = st.text_area("전체 소재 목록", height=260, key=f"pool-items-{pool.id}", help="검색 결과와 관계없이 전체 목록을 편집합니다. 한 줄에 소재 하나를 입력하세요.")
            instruction = st.text_area("생성 지침", height=100, key=f"pool-instruction-{pool.id}")
            active = st.checkbox("이 풀 사용", key=f"pool-active-{pool.id}")
            slots = st.text_input("연결 문항 (PBT 기준)", key=f"pool-slots-{pool.id}", help="IBT 대응 번호는 자동 연결됩니다. 공통 지문·대본의 문항은 같은 풀에 함께 지정하세요.")
            if st.form_submit_button("소재 풀 저장", type="primary"):
                try:
                    updated = SlotTopicPool.model_validate({
                        **pool.model_dump(), "name": name.strip(), "items": parse_pool_items(items),
                        "prompt_instruction": instruction.strip(), "active": active,
                        "slot_numbers": parse_slot_numbers(slots),
                    })
                    store.save_slot_pool(updated)
                    saved("소재 풀을 저장했습니다. 연결된 PBT·IBT 문항에 함께 적용됩니다.")
                except ValueError as exc:
                    st.error(str(exc))
    usages = [row for row in store.list_slot_pool_usages(5000) if row["pool_id"] == pool.id]
    if usages:
        with st.expander("이 풀의 최근 사용 이력"):
            st.dataframe([{"시각": row["created_at"], "소재": row["item"], "유형": row["question_type"], "모델": row["provider"]} for row in usages[:100]], hide_index=True, width="stretch")


def render_topics(store: TopicStore, topics: list[TopicDefinition], context: str) -> None:
    # Keep selection stable when a title or domain changes.
    topics = sorted(topics, key=lambda topic: topic.id)
    st.subheader("공용 소재")
    st.caption("유형에 맞는 소재에서 최근 50개 소재·관점 조합을 제외하고, 분야와 사용 횟수를 고려해 선택합니다.")
    query = st.text_input("공용 소재 검색", key=f"topic-search-{context}")
    filtered = [topic for topic in topics if query.casefold() in " ".join([topic.domain, topic.title, *topic.angles]).casefold()]
    st.caption(f"대상 소재 {len(topics)}개 · 검색 결과 {len(filtered)}개 · 행을 선택하면 수정할 수 있습니다.")
    if not filtered:
        st.info("조건에 맞는 공용 소재가 없습니다. 전체 소재 풀에서 비활성 소재와 적용 유형을 확인하거나 새 소재를 추가하세요.")
        return
    index = selected_row([
        {"소재": topic.title, "분야": topic.domain, "접근 관점": " / ".join(topic.angles), "상태": "사용 중" if topic.active else "사용 안 함"}
        for topic in filtered
    ], f"topic-items-{context}-{query}", height=300)
    topic = filtered[index]
    with st.expander(f"선택 소재 수정 · {topic.title}"):
        with st.form(f"edit-topic-{topic.id}"):
            initialize_fields({
                f"topic-domain-{topic.id}": topic.domain, f"topic-title-{topic.id}": topic.title,
                f"topic-reading-{topic.id}": topic.reading_types, f"topic-listening-{topic.id}": topic.listening_types,
                f"topic-active-{topic.id}": topic.active,
                **{f"topic-angle-{topic.id}-{index}": value for index, value in enumerate(topic.angles, 1)},
            })
            domain = st.text_input("분야", key=f"topic-domain-{topic.id}")
            title = st.text_input("소재명", key=f"topic-title-{topic.id}")
            angles = [st.text_input(f"접근 관점 {index}", key=f"topic-angle-{topic.id}-{index}") for index in range(1, 4)]
            reading = st.multiselect("읽기 적용 유형", list(QUESTION_TYPE_PROFILES), format_func=lambda value: type_label("reading", value), key=f"topic-reading-{topic.id}")
            listening = st.multiselect("듣기 적용 유형", list(LISTENING_TYPE_PROFILES), format_func=lambda value: type_label("listening", value), key=f"topic-listening-{topic.id}")
            active = st.checkbox("이 소재 사용", key=f"topic-active-{topic.id}")
            if st.form_submit_button("공용 소재 저장", type="primary"):
                try:
                    updated = TopicDefinition.model_validate({**topic.model_dump(), "domain": domain.strip(), "title": title.strip(), "angles": angles, "reading_types": reading, "listening_types": listening, "active": active})
                    store.save_topic(updated)
                    saved("공용 소재를 저장했습니다.")
                except ValueError as exc:
                    st.error(str(exc))
    usages = [row for row in store.list_usages(5000) if row["topic_id"] == topic.id]
    if usages:
        with st.expander("선택 소재의 최근 사용 이력"):
            st.dataframe([{"시각": row["created_at"], "관점": row["angle"], "유형": row["question_type"], "모델": row["provider"]} for row in usages[:100]], hide_index=True, width="stretch")


def render_applications(store: TopicStore, section: str, exam: str, query: str) -> None:
    applications = applications_for_exam(store, section, exam)
    filtered = [application for application in applications if application_matches(application, query)]
    counts = {kind: sum(len(a.slots) for a in applications if a.resolved and a.resolved.source == kind)
              for kind in ("slot_pool", "global", "none")}
    st.caption(f"지정 풀 {counts['slot_pool']}문항 · 공용 소재 {counts['global']}문항 · 별도 소재 없음 {counts['none']}문항")
    if any(a.error for a in applications):
        st.warning("소재 연결을 확인해야 하는 문항이 있습니다. 해당 행을 선택해 설정을 확인하세요.")
    if not filtered:
        st.info("검색 조건에 맞는 문항이 없습니다.")
        return
    left, right = st.columns([1.2, 1.3], gap="medium")
    with left:
        st.subheader(f"{exam.upper()} {section_label(section)} 적용 현황")
        st.caption("행을 선택하면 오른쪽에 적용 소재가 열립니다. 공통 지문·대본은 한 행으로 표시합니다.")
        index = selected_row([application.row() for application in filtered], f"applications-{section}-{exam}-{query}", height=520)
    application = filtered[index]
    with right:
        st.caption(f"선택 문항 · {exam.upper()} {format_slot_numbers(application.slots)}번 · {application.label}")
        if application.error:
            st.error(application.error)
            matches = [pool for pool in store.list_slot_pools(section=section) if set(application.reference_slots) & set(pool.slot_numbers)]
            if matches:
                pool_id = st.selectbox("연결을 수정할 풀", [pool.id for pool in matches], format_func=lambda value: next(pool.name for pool in matches if pool.id == value))
                render_pool(store, next(pool for pool in matches if pool.id == pool_id))
        elif application.resolved.pool:
            render_pool(store, application.resolved.pool)
        elif application.resolved.source == "global":
            render_topics(store, application.resolved.topics, f"{section}-{application.question_type}")
        else:
            st.subheader("별도 소재 없음")
            st.info("이 문항은 별도의 소재 배정 없이 유형 지침과 기출 분석에 따라 생성합니다.")
        inactive = [pool for pool in store.list_slot_pools(section=section) if not pool.active and set(application.reference_slots) & set(pool.slot_numbers)]
        if inactive:
            st.caption("연결된 비활성 풀: " + ", ".join(pool.name for pool in inactive) + ". ‘전체 소재 풀’에서 확인·수정할 수 있습니다.")


def render_all_pools(store: TopicStore, section: str, exam: str, query: str) -> None:
    number = query_number(query)
    pools = [pool for pool in store.list_slot_pools(section=section)
             if (number in pool_exam_slots(pool, exam) if number is not None
                 else query.casefold() in " ".join([pool.name, *pool.items]).casefold())]
    topics = store.list_topics()
    show_global = not query or query in "공용 소재" or any(query.casefold() in " ".join([topic.domain, topic.title, *topic.angles]).casefold() for topic in topics)
    if number is not None:
        show_global = any(number in application.slots and application.resolved and application.resolved.source == "global"
                          for application in applications_for_exam(store, section, exam))
    rows = [{"소재 풀": pool.name, f"{exam.upper()} 연결 문항": format_slot_numbers(pool_exam_slots(pool, exam)) or "연결 없음", "소재 수": len(pool.items), "상태": "사용 중" if pool.active else "사용 안 함"} for pool in pools]
    if show_global:
        rows.append({"소재 풀": "공용 소재 전체", f"{exam.upper()} 연결 문항": "유형별 적용", "소재 수": len(topics), "상태": "소재별 설정"})
    if not rows:
        st.info("검색 조건에 맞는 풀이 없습니다.")
        return
    left, right = st.columns([1.2, 1.3], gap="medium")
    with left:
        st.subheader("전체 소재 풀")
        st.caption("사용하지 않는 풀도 표시합니다. 행을 선택해 내용과 연결 문항을 확인하세요.")
        index = selected_row(rows, f"all-pools-{section}-{exam}-{query}", height=520)
    with right:
        if index < len(pools):
            render_pool(store, pools[index])
        else:
            render_topics(store, topics, "all")


def render_additions(store: TopicStore, section: str, exam: str) -> None:
    with st.expander("새 소재 추가"):
        kind = st.radio("추가할 항목", ["지정 풀", "공용 소재"], horizontal=True)
        if kind == "지정 풀":
            applications = applications_for_exam(store, section, exam)
            with st.form("add-pool"):
                chosen = st.multiselect("연결할 문항", list(range(len(applications))), format_func=lambda i: f"{exam.upper()} {format_slot_numbers(applications[i].slots)}번 · {applications[i].label}")
                name = st.text_input("새 풀 이름")
                items = st.text_area("새 풀 소재 목록", help="한 줄에 소재 하나를 입력하세요.")
                instruction = st.text_area("새 풀 생성 지침")
                st.caption("선택한 문항의 PBT·IBT 대응 번호가 함께 연결됩니다. 기존 활성 풀이 있는 번호는 중복 지정할 수 없습니다.")
                if st.form_submit_button("새 지정 풀 저장"):
                    try:
                        references = sorted({n for i in chosen for n in applications[i].reference_slots})
                        store.save_slot_pool(SlotTopicPool(id=f"custom_pool_{uuid4().hex}", section=section, slot_numbers=references, name=name.strip(), items=parse_pool_items(items), prompt_instruction=instruction.strip()))
                        saved("새 소재 풀을 추가했습니다.")
                    except ValueError as exc:
                        st.error(str(exc))
        else:
            with st.form("add-topic"):
                domain = st.text_input("새 소재 분야")
                title = st.text_input("새 소재명")
                angles = [st.text_input(f"새 접근 관점 {index}") for index in range(1, 4)]
                reading = st.multiselect("새 소재 읽기 유형", list(QUESTION_TYPE_PROFILES), format_func=lambda value: type_label("reading", value))
                listening = st.multiselect("새 소재 듣기 유형", list(LISTENING_TYPE_PROFILES), format_func=lambda value: type_label("listening", value))
                if st.form_submit_button("새 공용 소재 저장"):
                    try:
                        store.save_topic(TopicDefinition(id=f"custom_{uuid4().hex}", domain=domain.strip(), title=title.strip(), angles=angles, reading_types=reading, listening_types=listening))
                        saved("새 공용 소재를 추가했습니다.")
                    except ValueError as exc:
                        st.error(str(exc))
    with st.expander("기본 소재 복원"):
        st.caption("누락된 기본값을 추가합니다. 기존 풀·소재의 수정 내용과 사용 이력은 유지합니다.")
        if st.button("누락된 기본 지정 풀 복원"):
            count = store.seed_default_slot_pools()
            saved(f"누락된 기본 풀 {count}개를 추가했습니다.")
        if st.button("누락된 기본 공용 소재 복원"):
            count = store.seed_defaults()
            saved(f"누락된 기본 공용 소재 {count}개를 추가했습니다.")


st.set_page_config(page_title="TOPIK 소재 은행", page_icon="🗂️", layout="wide")
st.markdown("<style>.block-container {padding-top: 2rem; padding-bottom: 2rem;}</style>", unsafe_allow_html=True)
st.title("소재 은행")
st.caption("시험과 문항을 선택해 실제 적용 풀, 전체 소재, 생성 지침을 한곳에서 확인하고 수정합니다.")
if message := st.session_state.pop("topic-save-message", ""):
    st.success(message)
topic_store = get_topic_store(TOPIC_STORE_SCHEMA_VERSION)
section_col, exam_col, view_col = st.columns([1, 1, 2])
section = section_col.radio("영역", ["listening", "reading"], format_func=section_label, horizontal=True, key="topic-section")
exam = exam_col.radio("시험 형식", ["pbt", "ibt"], format_func=str.upper, horizontal=True, key="topic-exam")
view = view_col.radio("보기", ["문항별 적용", "전체 소재 풀"], horizontal=True, key="topic-view")
query = st.text_input("문항·풀·소재 검색", placeholder="문항 번호, 유형, 풀 이름 또는 소재", key="topic-query").strip()
if view == "문항별 적용":
    render_applications(topic_store, section, exam, query)
else:
    render_all_pools(topic_store, section, exam, query)
st.divider()
render_additions(topic_store, section, exam)
