from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.step_06_publication.storage_overview import load_overview, REVIEW_LABELS, PHASE_LABELS
from topik_question_lab.step_06_publication.media_cloud import preview_s3_media
from topik_question_lab.step_06_publication.bank_editor import set_health


ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
load_dotenv(ROOT / ".env")
SECTION = {"reading": "읽기", "listening": "듣기", "writing": "쓰기"}
st.set_page_config(page_title="문제 저장 현황", page_icon="📊", layout="wide")
st.title("문제 저장 현황")
st.caption("SQLite 생성 기록과 PostgreSQL 문항 은행을 조회합니다. 실제 음원·이미지 파일은 S3에 저장됩니다.")
database_url = os.getenv("DATABASE_URL", "")
identity = (str(ROOT), hashlib.sha256(database_url.encode()).hexdigest())
refresh = st.button("현황 새로고침", type="primary")
saved = st.session_state.get("storage-overview")
if refresh or not saved or saved["identity"] != identity:
    with st.spinner("저장 현황을 읽고 있습니다."):
        saved = {"identity": identity, "snapshot": load_overview(ROOT, database_url)}
        st.session_state["storage-overview"] = saved
snapshot = saved["snapshot"]
bank, all_rows = snapshot["bank"], snapshot["rows"]
st.caption(f"조회 시각: {snapshot['checked_at']} · UTC · 생성 중인 작업은 새로고침으로 확인하세요.")
st.caption("DB별로 조회한 시점의 기록입니다. 실행 중에는 SQLite와 PostgreSQL의 반영 시점이 잠시 다를 수 있습니다.")
for error in snapshot["errors"]:
    st.warning(error)
if not bank["available"] or not bank["media_available"]:
    st.warning(bank["message"])

st.subheader("전체 저장 현황")
st.caption(f"휴지통 {sum(i.get('trashed_questions', 0) for i in snapshot['inventory'])}문항은 별도 보관하며 아래 집계에서 제외합니다.")
metrics = st.columns(5)
metrics[0].metric("SQLite 생성 문항", sum(i["generated_questions"] for i in snapshot["inventory"]))
metrics[1].metric("PostgreSQL 문항", len(bank["items"]) if bank["available"] else "확인 불가")
metrics[2].metric("PostgreSQL 세트", len(bank["sets"]) if bank["available"] else "확인 불가")
metrics[3].metric("미등록 문항", sum(r["status"] == "미등록" for r in all_rows)
                  if bank["available"] and not snapshot["errors"] else "확인 불가")
metrics[4].metric("참고 예시", sum(i["examples"] for i in snapshot["inventory"]))
reviews = Counter(i["review_status"] for i in bank["items"])
st.caption("PostgreSQL 검수 상태: " + (" · ".join(f"{REVIEW_LABELS.get(k, k)} {n}문항" for k, n in reviews.items())
                                    if bank["available"] else "확인 불가"))

filters = st.columns(3)
sections = filters[0].multiselect("영역", list(SECTION), format_func=SECTION.get)
models = filters[1].multiselect("모델", sorted({r["model_id"] for r in all_rows} | {b["model"] for b in snapshot["batches"]}))
date_filter = filters[2].checkbox("날짜 범위 지정")
today = datetime.now(timezone.utc).date()
dates = st.date_input("생성일 / 세트 등록일 (UTC)", value=(today, today)) if date_filter else ()


def included(section, model, created):
    if sections and section not in sections or models and model not in models:
        return False
    if date_filter and isinstance(dates, (tuple, list)) and len(dates) == 2:
        try:
            day = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if day.tzinfo:
                day = day.astimezone(timezone.utc)
            return dates[0] <= day.date() <= dates[1]
        except (ValueError, TypeError):
            return False
    return True


def fraction(value, total):
    return "확인 불가" if value is None or total is None else f"{value}/{total}"


def coverage_text(value):
    if value is None:
        return "확인 불가"
    return f"음원 {value['audio_files']}개 · 연결 {value['audio_bound']}/{value['audio_required']}문항 · 이미지 {value['images_ready']}/{value['images_required']}"


def preview_url(value):
    # Only display the configured public CDN; never expose signed credentials.
    try:
        url, base = urlsplit(value), urlsplit(os.getenv("CLOUDFRONT_BASE_URL", ""))
        if (url.scheme == "https" and url.netloc == base.netloc and url.hostname
                and not url.username and not url.password and not url.query and not url.fragment
                and url.path.startswith(base.path.rstrip("/") + "/")):
            return value
    except ValueError:
        pass
    return None


def show_question(column, question):
    shown = set()
    for field, label in (("passage", "지문"), ("auxiliary_text", "자료"), ("stem", "문제"), ("question_prompt", "질문")):
        text = question.get(field)
        if text and text not in shown:
            column.markdown(f"**{label}**")
            column.write(text)
            shown.add(text)
    if question.get("dialogue_turns"):
        column.markdown("**듣기 대본**")
        for turn in question["dialogue_turns"]:
            column.write(f"{turn.get('speaker', '')}: {turn.get('text', '')}")
    column.markdown("**보기**")
    for number, choice in enumerate(question.get("choices") or [], 1):
        column.write(f"{number}. {choice}")
    for option in question.get("visual_options") or []:
        column.write(f"{option.get('number', '')}. {option.get('description', '')}")
    column.write(f"정답: {question.get('answer', '미기록')}")
    column.write(f"해설: {question.get('explanation') or '미기록'}")
    with column.expander("저장 데이터 원문"):
        st.json(question)


batch_tab, item_tab, storage_tab = st.tabs(["생성 묶음", "문항 조회·비교", "저장소·세트"])
with batch_tab:
    batches = [b for b in snapshot["batches"] if included(b["section"], b["model"], b["created_at"])]
    st.caption("미디어 준비는 현재 문제와 일치하는 생성·검증 작업 기록입니다. 실제 파일의 현재 접근 여부는 세트별 검증으로 확인하세요.")
    st.caption("듣기 50문항은 35개 음원을 공유합니다. 이미지 수는 필요한 문항별 이미지 연결 수이며, 미완성·검사 오류 묶음은 준비 수량을 확정할 수 없습니다.")
    if batches:
        st.dataframe([{
            "묶음": b["id"], "영역": SECTION.get(b["section"], b["section"]), "모델": b["model"],
            "생성일": b["created_at"], "SQLite 문항": f"{b['local_count']}/50",
            "음원 파일": b["preparation"]["audio_files"],
            "음원 준비 문항": fraction(b["preparation"]["audio_bound"], 50) if b["section"] == "listening" else "해당 없음",
            "이미지 준비": fraction(b["preparation"]["images_ready"], b["preparation"]["images_required"]),
            "PG 등록": fraction(b["registered_count"], 50),
            "PG 미디어 연결": coverage_text(b["coverage"]),
            "처리 단계": PHASE_LABELS.get(b.get("pipeline_status") or b["status"], b.get("pipeline_status") or b["status"]),
            "재시도 대기": b["task_statuses"].get("retry_wait", 0), "실패 작업": b["task_statuses"].get("failed", 0),
            "PG 검수": b["review_status"] if bank["available"] else "확인 불가",
        } for b in batches], hide_index=True, width="stretch")
        bid = st.selectbox("작업 기록을 볼 묶음", [b["id"] for b in batches])
        batch = next(b for b in batches if b["id"] == bid)
        with st.expander("미디어 작업·재시도 기록"):
            st.dataframe([{"단계": PHASE_LABELS.get(t["kind"], t["kind"]),
                           "상태": PHASE_LABELS.get(t["status"], t["status"]), "시도 횟수": t["attempts"],
                           "재시도 예정 (UTC)": datetime.fromtimestamp(t["next_attempt_at"], timezone.utc).isoformat(timespec="seconds")
                           if t["next_attempt_at"] else "—"} for t in batch["tasks"]], hide_index=True, width="stretch")
        st.caption("재시도·중단은 ‘50문제 일괄 생성’ 화면에서 실행할 수 있습니다.")
    else:
        st.info("조건에 맞는 일괄 생성 묶음이 없습니다.")

with item_tab:
    statuses = st.multiselect("저장 상태", sorted({r["status"] for r in all_rows}))
    rows = [r for r in all_rows if included(r["section"], r["model_id"], r["created_at"])
            and (not statuses or r["status"] in statuses)]
    st.caption(f"필터 결과 {len(rows)}문항 · 저장 내용 비교와 검수 상태는 별도로 표시합니다.")
    if rows:
        st.dataframe([{"상태": r["status"], "영역": SECTION.get(r["section"], r["section"]), "모델": r["model_id"],
                       "번호": r["type_slot"], "유형": r["item_type"], "생성일": r["created_at"],
                       "SQLite 검수": r["local_review"], "PG 검수": r["postgres_review"],
                       "미디어 URL 연결": coverage_text(r["coverage"]), "세트": r["set_id"], "문항 키": r["source_key"]}
                      for r in rows], hide_index=True, width="stretch")
        key = st.selectbox("상세 문항", [r["source_key"] for r in rows])
        selected = next(r for r in rows if r["source_key"] == key)
        if selected["postgres_question"] is not None:
            if st.button("이 문제 PostgreSQL에서 편집"):
                stored = next(i for i in bank["items"] if i["source_key"] == key)
                st.session_state["bank-editor-open-item"] = str(stored["item_id"])
                st.switch_page(str(Path(__file__).with_name("bank_editor_app.py")))
        left, right = st.columns(2)
        for column, label, field in ((left, "SQLite 현재 내용", "local_question"), (right, "PostgreSQL 저장 내용", "postgres_question")):
            column.markdown(f"**{label}**")
            if selected[field] is not None:
                show_question(column, selected[field])
            else:
                column.info("조회된 문항이 없습니다." if bank["available"] or field == "local_question" else "PostgreSQL 확인 불가")
        if selected["reason"] and selected["local_present"]:
            st.caption(f"로컬 검수 참고: {selected['reason']}")
        if selected["status"] == "내용 불일치":
            st.info("SQLite와 PostgreSQL 내용이 다릅니다. PostgreSQL에서 직접 편집했을 수도 있으므로 양쪽 내용을 비교하세요.")
        assets = ([selected["audio"]] if selected["audio"] else []) + selected["visuals"]
        if assets:
            st.caption("URL 연결은 확인했지만 실제 파일 접근은 아직 검사하지 않았습니다.")
            show_media = st.checkbox("선택 문항 음원·이미지 불러오기", key=f"storage-media-{key}")
            for asset in assets:
                url = preview_url(asset["storage_url"])
                label = "음원" if "audio_asset_id" in asset else f"이미지 · {asset['visual_role']} {asset['option_number']}"
                if url:
                    st.link_button(f"{label} URL 열기", url)
                    if show_media:
                        if "audio_asset_id" in asset:
                            st.audio(url)
                        else:
                            st.image(url, caption=label)
                else:
                    st.caption(f"{label}: URL이 등록되어 있습니다. 미리보기는 설정된 CloudFront 공개 주소를 지원합니다.")
    else:
        st.info("조건에 맞는 문항이 없습니다.")

with storage_tab:
    st.subheader("SQLite 파일별 저장 수량")
    st.dataframe([{"영역": SECTION[i["section"]], "DB 파일": i["path"], "생성 문항": i["generated_questions"], "휴지통": i.get("trashed_questions", 0), "참고 예시": i["examples"]}
                  for i in snapshot["inventory"] if not sections or i["section"] in sections], hide_index=True, width="stretch")
    st.caption("파일별 수량은 영역 필터만 적용됩니다. 참고 예시는 생성 문항 수에 포함하지 않습니다.")
    st.subheader("PostgreSQL 세트")
    sets = [s for s in bank["sets"] if included(s["section"], s["generator_version"], s["published_at"])]
    st.dataframe([{"세트": str(s["set_id"]), "영역": SECTION.get(s["section"], s["section"]), "모델": s["generator_version"],
                   "문항": f"{s['item_count']}/50", "검수": REVIEW_LABELS.get(s["review_status"], s["review_status"]),
                   "구성 상태": set_health([i for i in bank["items"] if i["set_id"] == s["set_id"]])["label"],
                   "미디어 URL 연결": coverage_text(s["coverage"]), "등록일": str(s["published_at"])} for s in sets],
                 hide_index=True, width="stretch")
    if sets and bank["media_available"]:
        labels = {str(s["set_id"]): f"{SECTION.get(s['section'], s['section'])} · {s['generator_version']} · 세트 {s['set_sequence']} · {s['set_id']}" for s in sets}
        set_id = st.selectbox("실제 파일을 검증할 세트", list(labels), format_func=labels.get)
        st.caption("버튼을 누르면 해당 세트의 S3·CloudFront 파일 크기·해시·음원 부분 요청을 검사합니다. 검증 결과는 이 화면에만 보관합니다.")
        check_key = (snapshot["checked_at"], identity, set_id)
        if st.button("선택 세트 S3·CloudFront 검증"):
            with st.spinner("선택 세트의 실제 파일을 검증하고 있습니다."):
                try:
                    result = preview_s3_media(database_url, [set_id])
                    message = f"실제 파일 검증 통과 · {result['items']}문항 · {result['files']}파일"
                    success = True
                except Exception:
                    message = "검증 실패 · 세트 구성, 필수 미디어 연결 및 S3·CloudFront 접근 설정을 확인하세요."
                    success = False
                st.session_state["storage-file-check"] = (check_key, success, message, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        checked = st.session_state.get("storage-file-check")
        if checked and checked[0] == check_key:
            (st.success if checked[1] else st.warning)(f"{checked[2]} · {checked[3]}")
        else:
            st.info("실제 파일 접근: 미검증")
    with st.expander("SQLite DB 등록 작업 이력"):
        st.dataframe([{"작업 ID": j["id"], "상태": PHASE_LABELS.get(j["status"], j["status"]),
                       "생성일": j["created_at"], "변경일": j["updated_at"]} for j in snapshot["jobs"]], hide_index=True, width="stretch")
