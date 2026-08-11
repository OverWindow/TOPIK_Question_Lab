from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(PROJECT_ROOT))).resolve()
load_dotenv(ROOT / ".env")

from topik_question_lab.postgres_storage import PostgresQuestionBank, PostgresUnavailableError
from topik_question_lab.providers import provider_label
from topik_question_lab.publication import (
    PublicationCandidate,
    PublicationItemMetadata,
    build_item_drafts,
    build_set_draft,
    collect_candidate_catalog,
    default_primary_skill,
    group_by_slot,
    reconcile_catalog,
)


SECTION_LABELS = {"reading": "읽기", "listening": "듣기", "writing": "쓰기"}


def identity_label(identity: tuple[str, str, str]) -> str:
    section, provider, model = identity
    return f"{SECTION_LABELS.get(section, section)} · {provider_label(provider)} · {model}"


def identity_key(identity: tuple[str, str, str]) -> str:
    return hashlib.sha256(":".join(identity).encode("utf-8")).hexdigest()[:12]


def metadata_rows(
    candidates: list[PublicationCandidate],
    published_by_source: dict[str, dict],
    default_level: int,
    default_difficulty: float,
) -> list[dict]:
    rows = []
    for candidate in sorted(candidates, key=lambda value: (value.slot, value.generated_id)):
        remote = published_by_source.get(candidate.source_key, {})
        rows.append(
            {
                "source_key": candidate.source_key,
                "번호": candidate.slot,
                "유형": candidate.question_type,
                "target_level": int(remote.get("target_level", default_level)),
                "predicted_difficulty": float(remote.get("predicted_difficulty", default_difficulty)),
                "primary_skill": str(remote.get("primary_skill") or default_primary_skill(candidate)),
                "review_status": str(remote.get("review_status", "reviewed")),
            }
        )
    return rows


def metadata_editor(rows: list[dict], key: str):
    return st.data_editor(
        rows,
        hide_index=True,
        width="stretch",
        disabled=["source_key", "번호", "유형", "review_status"],
        column_config={
            "source_key": None,
            "target_level": st.column_config.NumberColumn(
                "TOPIK 급수", min_value=3, max_value=6, step=1, required=True
            ),
            "predicted_difficulty": st.column_config.NumberColumn(
                "예상 난이도", min_value=-3.0, max_value=3.0, step=0.1, required=True
            ),
            "primary_skill": st.column_config.TextColumn("대표 Skill", required=True),
            "review_status": st.column_config.TextColumn("검수 상태"),
        },
        key=key,
    )


def metadata_map(edited) -> dict[str, PublicationItemMetadata]:
    rows = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited)
    return {
        str(row["source_key"]): PublicationItemMetadata(
            target_level=int(row["target_level"]),
            predicted_difficulty=float(row["predicted_difficulty"]),
            primary_skill=str(row["primary_skill"]),
        )
        for row in rows
    }


def csv_bytes(rows: list[dict]) -> bytes:
    fields = [
        "source_key", "section", "provider_key", "model_id", "type_slot", "item_type",
        "local_id", "status", "reason", "item_id", "item_version", "published_at", "in_set",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows({key: value.get(key) for key in fields} for value in rows)
    return output.getvalue().encode("utf-8-sig")


st.set_page_config(page_title="PostgreSQL 문항 발행", page_icon="🗄️", layout="wide")
st.title("PostgreSQL 문항 은행 발행")
st.caption("문제 전체 내용과 모델·영역별 세트를 PostgreSQL에 저장하고 로컬 SQLite와 동기화 상태를 확인합니다.")

database_url = os.getenv("DATABASE_URL", "").strip()
bank = PostgresQuestionBank(database_url) if database_url else None

st.subheader("연결 및 스키마")
if not database_url:
    st.info("`.env`에 `DATABASE_URL`을 설정하면 PostgreSQL 문항 은행을 사용할 수 있습니다.")
else:
    if st.button("연결 확인 및 스키마 준비", type="primary"):
        try:
            database_name = bank.check_connection()
            applied = bank.ensure_schema()
            st.session_state["postgres-schema-ready"] = True
            suffix = f" 적용된 마이그레이션: {', '.join(applied)}" if applied else " 스키마가 최신 상태입니다."
            st.success(f"PostgreSQL `{database_name}` 연결을 확인했습니다.{suffix}")
        except PostgresUnavailableError as exc:
            st.session_state["postgres-schema-ready"] = False
            st.error(str(exc))

schema_ready = bool(bank and st.session_state.get("postgres-schema-ready"))
catalog = collect_candidate_catalog(ROOT)
identities = catalog.identities()
published_items: list[dict] = []
set_contents: list[dict] = []
if schema_ready:
    try:
        published_items = bank.list_current_items()
        set_contents = bank.list_current_set_contents()
    except PostgresUnavailableError as exc:
        st.warning(str(exc))
        schema_ready = False
published_by_source = {str(value["source_key"]): value for value in published_items}
set_source_keys = {str(value["source_key"]) for value in set_contents}
reconciliation_rows = reconcile_catalog(catalog, published_items, set_source_keys) if schema_ready else []
sync_status_by_source = {str(value["source_key"]): str(value["status"]) for value in reconciliation_rows}

sync_tab, set_tab, status_tab, history_tab = st.tabs(
    ["문항 동기화", "50문항 세트", "이관 현황", "발행 이력"]
)

with sync_tab:
    st.subheader("승인 문항 동기화")
    st.caption("세트 완성 여부와 관계없이 최종 승인되고 자동검사를 통과한 문제 전체를 PostgreSQL 문항 은행에 저장합니다.")
    sync_identity = st.selectbox("모델·영역", identities, format_func=identity_label, key="item-sync-identity")
    sync_candidates = catalog.for_identity(*sync_identity)
    sync_key = identity_key(sync_identity)
    summary = st.columns(3)
    summary[0].metric("동기화 가능", len(sync_candidates))
    summary[1].metric("최신", sum(sync_status_by_source.get(value.source_key) == "최신" for value in sync_candidates))
    summary[2].metric(
        "동기화 필요",
        sum(sync_status_by_source.get(value.source_key) in {"미발행", "수정 후 미동기"} for value in sync_candidates),
    )
    settings = st.columns(2)
    sync_level = settings[0].selectbox("기본 TOPIK 급수", [3, 4, 5, 6], index=1, key=f"sync-level-{sync_key}")
    sync_difficulty = float(
        settings[1].number_input(
            "기본 예상 난이도 (-3~+3)", -3.0, 3.0, 0.0, 0.1, format="%.1f", key=f"sync-difficulty-{sync_key}"
        )
    )
    sync_rows = metadata_rows(sync_candidates, published_by_source, sync_level, sync_difficulty)
    sync_metadata = metadata_editor(sync_rows, f"sync-metadata-{sync_key}-{sync_level}-{sync_difficulty}")
    if st.button("선택 모델·영역 승인 문항 동기화", type="primary", disabled=not schema_ready):
        try:
            drafts = build_item_drafts(
                sync_candidates,
                metadata_map(sync_metadata),
                sync_level,
                sync_difficulty,
            )
            receipt = bank.publish_items(drafts)
            st.success(
                f"{receipt.total_items}개를 처리했습니다. 신규 문항 버전 {receipt.created_item_versions}개 · "
                f"기존 버전 재사용 {receipt.reused_item_versions}개"
            )
        except (ValueError, PostgresUnavailableError) as exc:
            st.error(str(exc))

with set_tab:
    st.subheader("모델·영역별 1~50번 세트")
    set_identity = st.selectbox("세트 모델·영역", identities, format_func=identity_label, key="set-publish-identity")
    set_candidates = catalog.for_identity(*set_identity)
    grouped = group_by_slot(set_candidates)
    missing = [slot for slot, values in grouped.items() if not values]
    duplicates = [slot for slot, values in grouped.items() if len(values) > 1]
    set_summary = st.columns(4)
    set_summary[0].metric("승인·검사 통과", len(set_candidates))
    set_summary[1].metric("채워진 번호", 50 - len(missing))
    set_summary[2].metric("누락 번호", len(missing))
    set_summary[3].metric("복수 후보 번호", len(duplicates))
    if missing:
        st.error(f"1~50번이 완성되지 않아 세트로 발행할 수 없습니다. 누락: {missing}")
    if duplicates:
        st.warning(f"복수 후보 중 세트에 넣을 문항을 선택하십시오: {duplicates}")
    set_key = identity_key(set_identity)
    selected_candidates: list[PublicationCandidate] = []
    for slot in range(1, 51):
        values = grouped[slot]
        if not values:
            continue
        if len(values) == 1:
            selected_candidates.append(values[0])
        else:
            selected_candidates.append(
                st.selectbox(
                    f"{slot}번 후보", values, format_func=lambda value: value.label,
                    key=f"set-candidate-{set_key}-{slot}",
                )
            )
    with st.expander("세트 1~50번 미리보기", expanded=not missing):
        st.dataframe(
            [
                {
                    "번호": value.slot,
                    "유형": value.question_type,
                    "source_key": value.source_key,
                    "출제 포인트": default_primary_skill(value),
                }
                for value in sorted(selected_candidates, key=lambda candidate: candidate.slot)
            ],
            hide_index=True,
            width="stretch",
        )
    set_settings = st.columns(2)
    set_level = set_settings[0].selectbox("세트 기본 TOPIK 급수", [3, 4, 5, 6], index=1, key=f"set-level-{set_key}")
    set_difficulty = float(
        set_settings[1].number_input(
            "세트 기본 예상 난이도 (-3~+3)", -3.0, 3.0, 0.0, 0.1, format="%.1f", key=f"set-difficulty-{set_key}"
        )
    )
    set_rows = metadata_rows(selected_candidates, published_by_source, set_level, set_difficulty)
    set_metadata = metadata_editor(set_rows, f"set-metadata-{set_key}-{set_level}-{set_difficulty}")
    can_publish_set = schema_ready and not missing and len(selected_candidates) == 50
    if st.button("PostgreSQL에 50문항 발행", type="primary", disabled=not can_publish_set):
        try:
            draft = build_set_draft(
                selected_candidates,
                metadata_map(set_metadata),
                set_level,
                set_difficulty,
            )
            receipt = bank.publish_set(draft)
            action = f"세트 v{receipt.set_version}을 발행했습니다." if receipt.created_set_version else f"동일한 세트 v{receipt.set_version}이 이미 있습니다."
            st.success(
                f"{action} set_id={receipt.set_id} · 신규 문항 버전 {receipt.created_item_versions}개 · "
                f"기존 버전 재사용 {receipt.reused_item_versions}개"
            )
        except (ValueError, PostgresUnavailableError) as exc:
            st.error(str(exc))

with status_tab:
    st.subheader("SQLite ↔ PostgreSQL 이관 현황")
    if not schema_ready:
        st.info("연결 확인 및 스키마 준비 후 이관 현황을 조회할 수 있습니다.")
    else:
        reconciliation = reconciliation_rows
        statuses = sorted({value["status"] for value in reconciliation})
        status_counts = {status: sum(value["status"] == status for value in reconciliation) for status in statuses}
        st.dataframe(
            [{"상태": status, "문항 수": count} for status, count in status_counts.items()],
            hide_index=True,
            width="stretch",
        )
        filters = st.columns(4)
        selected_statuses = filters[0].multiselect("상태", statuses, default=statuses)
        selected_sections = filters[1].multiselect(
            "영역", sorted({value["section"] for value in reconciliation}),
            format_func=lambda value: SECTION_LABELS.get(value, value),
        )
        selected_models = filters[2].multiselect("모델", sorted({value["model_id"] for value in reconciliation}))
        membership = filters[3].selectbox("세트 포함", ["전체", "포함", "미포함"])
        filtered = [
            value for value in reconciliation
            if value["status"] in selected_statuses
            and (not selected_sections or value["section"] in selected_sections)
            and (not selected_models or value["model_id"] in selected_models)
            and (membership == "전체" or bool(value["in_set"]) == (membership == "포함"))
        ]
        display_rows = [
            {
                "상태": value["status"],
                "영역": SECTION_LABELS.get(value["section"], value["section"]),
                "모델": value["model_id"],
                "번호": value["type_slot"],
                "유형": value["item_type"],
                "로컬 ID": value["local_id"],
                "PG item ID": value["item_id"],
                "버전": value["item_version"],
                "세트 포함": value["in_set"],
                "발행 시각": value["published_at"],
                "source_key": value["source_key"],
            }
            for value in filtered
        ]
        st.metric("필터 결과", len(display_rows))
        st.download_button(
            "이관 현황 CSV 다운로드", csv_bytes(filtered), "topik_postgres_sync_status.csv", "text/csv"
        )
        st.dataframe(display_rows, hide_index=True, width="stretch")
        if filtered:
            selected_key = st.selectbox(
                "상세 비교 문항",
                [value["source_key"] for value in filtered],
                format_func=lambda key: next(
                    f"{value['status']} · {value['model_id']} · {value['type_slot']}번 · {key}"
                    for value in filtered if value["source_key"] == key
                ),
            )
            selected = next(value for value in filtered if value["source_key"] == selected_key)
            left, right = st.columns(2)
            left.markdown("**로컬 SQLite**")
            left.code(json.dumps(selected["local_question"], ensure_ascii=False, indent=2, default=str), language="json")
            right.markdown("**PostgreSQL 최신 버전**")
            right.code(json.dumps(selected["postgres_question"], ensure_ascii=False, indent=2, default=str), language="json")
            if selected["reason"]:
                st.warning(selected["reason"])

with history_tab:
    st.subheader("최근 세트 발행 이력")
    if not schema_ready:
        st.info("연결 확인 및 스키마 준비 후 발행 이력을 조회할 수 있습니다.")
    else:
        try:
            history = bank.list_recent_sets()
            if history:
                st.dataframe(history, hide_index=True, width="stretch")
            else:
                st.info("아직 발행된 세트가 없습니다.")
        except PostgresUnavailableError as exc:
            st.warning(str(exc))
