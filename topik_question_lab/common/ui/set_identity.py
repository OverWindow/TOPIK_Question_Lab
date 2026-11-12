"""Consistent set names and summaries across generation and bank screens."""
from datetime import datetime, timedelta, timezone

import streamlit as st

from topik_question_lab.common.generation.exam_profiles import question_count
from topik_question_lab.common.question_bank.direct_publication import batch_set_id
from topik_question_lab.common.question_bank.storage_overview import REVIEW_LABELS


KST = timezone(timedelta(hours=9))
SECTION = {"reading": "읽기", "listening": "듣기", "writing": "쓰기"}


def korean_datetime(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # SQLite timestamps without an offset are stored in UTC.
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(KST)
    except (ValueError, TypeError):
        return None


def korean_time(value):
    parsed = korean_datetime(value)
    return parsed.strftime("%Y/%m/%d %H:%M") if parsed else "기록 없음"


def set_view(record, *, batches=(), sets=(), bank_available=True):
    """Join existing identities for display without changing the source records."""
    is_batch = "id" in record
    sid = str(record.get("set_id") or batch_set_id(record["section"], record["id"])) if is_batch else str(record["set_id"])
    batch = record if is_batch else next(
        (b for b in batches if str(b.get("set_id") or batch_set_id(b["section"], b["id"])) == sid), {})
    stored = next((s for s in sets if str(s["set_id"]) == sid), {}) if is_batch else record
    registered = bank_available and bool(stored)
    return {
        **record, "set_id": sid, "code": sid.replace("-", "")[:8].upper(),
        "model": batch.get("model") or stored.get("generator_version", "모델 미기록"),
        "generation_created_at": batch.get("created_at"),
        "published_at": stored.get("published_at"), "set_sequence": stored.get("set_sequence"),
        "bank_available": bank_available, "registered": registered,
        "count": stored.get("item_count", batch.get("registered_count")) if registered else batch.get("local_count"),
        "review": REVIEW_LABELS.get(stored.get("review_status"), stored.get("review_status", "")) if registered else "",
        "coverage": stored.get("coverage", batch.get("coverage")) if registered else None,
        "preparation": batch.get("preparation"),
        "local_approved_count": batch.get("local_approved_count"),
    }


def set_title(view):
    title = f"{view.get('exam_format', 'pbt').upper()} {SECTION.get(view['section'], view['section'])}"
    if korean_datetime(view.get("generation_created_at")):
        return f"{title} · {korean_time(view['generation_created_at'])} 생성"
    if view.get("set_sequence"):
        return f"{title} · 등록 세트 {view['set_sequence']}번"
    return f"{title} · 생성 시각 미기록"


def set_label(view):
    sequence = f" · 등록 {view['set_sequence']}번" if view.get("set_sequence") and korean_datetime(view.get("generation_created_at")) else ""
    return f"{set_title(view)} · {view['model']} · {view['code']}{sequence}"


def set_status(view):
    total = question_count(view.get("exam_format", "pbt"))
    count = view.get("count")
    quantity = f"문항 {count}/{total}" if count is not None else "문항 수 확인 불가"
    approved = view.get("local_approved_count")
    review = f"검수 {view['review']}" if view.get("review") else (
        f"검수 승인 {approved}/{total}" if approved is not None else "검수 확인 필요")
    coverage = view.get("coverage")
    media = "미디어 확인 불가"
    if coverage is not None:
        ready = (coverage["audio_bound"] == coverage["audio_required"]
                 and coverage["images_ready"] == coverage["images_required"] and count == total)
        media = "미디어 연결 완료" if ready else "미디어 연결 미완료"
    elif not view["registered"] and view.get("preparation"):
        prepared = view["preparation"]
        images = prepared.get("images_ready")
        audio = prepared.get("audio_bound")
        if images is not None and prepared.get("images_required") is not None and (view["section"] != "listening" or audio is not None):
            ready = images == prepared["images_required"] and (view["section"] != "listening" or audio == total) and count == total
            media = "미디어 준비 완료" if ready else "미디어 준비 중"
    registration = "등록 완료" if view["registered"] else "미등록" if view["bank_available"] else "등록 확인 불가"
    if view["registered"] and count is not None and count != total:
        registration = "등록됨 · 구성 미완성"
    return " · ".join((quantity, review, media, registration))


def set_list_row(view):
    return {"세트": set_title(view), "식별코드": view["code"], "모델": view["model"],
            "등록 번호": str(view["set_sequence"]) if view.get("set_sequence") else "—", "상태 요약": set_status(view)}


def render_set_summary(view):
    with st.container(border=True):
        st.subheader(set_title(view))
        sequence = f"등록 세트 {view['set_sequence']}번" if view.get("set_sequence") else "등록 번호 미확정"
        st.caption(f"{view['model']} · 식별코드 {view['code']} · {sequence} · 한국 시간 (KST)")
        if not view.get("generation_created_at") and view.get("published_at"):
            st.caption(f"생성 시각 미기록 · 등록일 {korean_time(view['published_at'])} (KST)")
        st.write(set_status(view))
        st.caption("미디어 준비·연결 상태입니다. 실제 파일 접근 여부는 별도 검증으로 확인합니다.")
