from pathlib import Path
import os

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.step_06_publication.bank_transfer import (
    TransferError, capture_source, check_shared_media, digest, execute_transfer, initialize_target,
    list_source_sets, prepare_transfer,
)


ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="서버에 문제 등록", page_icon="📡", layout="wide")
st.title("서버에 문제 등록")
st.caption("생성기 PostgreSQL → 선택 서버로 복사합니다. 기존 내용은 원본으로 덮어쓰고 S3·CloudFront 주소는 유지합니다.")

source = os.getenv("DATABASE_URL", "").strip()
targets = {
    "로컬 서버": os.getenv("QUESTION_BANK_LOCAL_DATABASE_URL", "").strip(),
    "운영 서버": os.getenv("QUESTION_BANK_PRODUCTION_DATABASE_URL", "").strip(),
}
selection = st.multiselect("등록 대상", list(targets), default=["로컬 서버"])
mode = st.radio("등록 범위", ["선택 세트", "전체 문제은행"], horizontal=True)
config_key = digest((source, targets))
if st.session_state.get("bank-transfer-config") != config_key:
    for key in ("bank-transfer-preview", "bank-transfer-sets", "bank-transfer-result"):
        st.session_state.pop(key, None)
    st.session_state["bank-transfer-config"] = config_key

configured = bool(source and selection and all(targets[label] for label in selection))
if not configured:
    st.info("DATABASE_URL과 선택 대상의 QUESTION_BANK_LOCAL_DATABASE_URL / QUESTION_BANK_PRODUCTION_DATABASE_URL을 설정하세요.")

if st.button("원본 세트 불러오기", disabled=not source):
    try:
        st.session_state["bank-transfer-sets"] = list_source_sets(source)
    except TransferError as exc:
        st.error(str(exc))
catalog = {r["set_id"]: r for r in st.session_state.get("bank-transfer-sets", [])}


def set_label(set_id):
    r = catalog[set_id]
    return f"{r['section']} · {r['model']} · {r['sequence']}번 세트 · {r['items']}문항 · {r['status']} · {set_id[:8]}"


selected_sets = st.multiselect("복사할 세트", list(catalog), format_func=set_label, disabled=mode == "전체 문제은행")
if mode == "전체 문제은행":
    st.caption("미배정 문항과 과거 미디어 기록도 포함합니다. 대상에만 있는 별도 세트는 유지합니다.")
st.caption("세트에서 빠진 기존 문항은 미배정으로 보존합니다. 원본의 검수 상태도 그대로 복사합니다.")
if "운영 서버" in selection:
    st.warning("운영 서버가 선택되어 있습니다. 아래 작업은 운영 DB에 적용됩니다.")

with st.expander("최초 대상 DB 구조 준비"):
    st.write("빈 대상 DB에 문제 테이블 5개와 조회 뷰 2개를 생성합니다. 기존 구조가 다르면 변경하지 않고 중단합니다.")
    if st.button("선택 서버 구조 초기화", disabled=not configured):
        for label in selection:
            try:
                created = initialize_target(source, targets[label])
                st.success(f"{label}: " + ("구조 생성 완료" if created else "이미 호환되는 구조입니다"))
            except TransferError as exc:
                st.error(f"{label}: {exc}")
        st.session_state.pop("bank-transfer-preview", None)

request_key = digest((config_key, selection, mode, sorted(selected_sets)))
if st.button("등록 내용 확인", disabled=not configured or (mode == "선택 세트" and not selected_sets)):
    try:
        with st.spinner("원본과 대상 내용을 확인합니다."):
            snapshot = capture_source(source, selected_sets if mode == "선택 세트" else None)
            media = check_shared_media(snapshot, os.getenv("CLOUDFRONT_BASE_URL", "").strip())
            plans, errors = {}, {}
            for label in selection:
                try:
                    plans[label] = prepare_transfer(snapshot, targets[label])
                except TransferError as exc:
                    errors[label] = str(exc)
            st.session_state["bank-transfer-preview"] = (request_key, plans, errors, media)
            st.session_state.pop("bank-transfer-result", None)
    except (TransferError, ValueError) as exc:
        st.session_state.pop("bank-transfer-preview", None)
        st.error(str(exc))

preview = st.session_state.get("bank-transfer-preview")
if preview and preview[0] == request_key:
    _, plans, errors, media = preview
    st.write(f"현재 미디어 접근 확인: {media['checked']}개 · 실패 {media['failed']}개 · 미확인 {media['unverified']}개 · 기존/로컬 주소 {media['legacy_or_local']}개")
    if media['failed'] or media['unverified'] or media['legacy_or_local']:
        st.warning("일부 미디어의 접근을 확인하지 못했습니다. DB 등록은 원본 주소·기록을 그대로 보존하며 파일을 수정하지 않습니다.")
    for label, message in errors.items():
        st.error(f"{label}: {message}")
    for label, plan in plans.items():
        st.subheader(label)
        st.dataframe([{"테이블": name, "복사할 행": count} for name, count in plan.snapshot.counts.items()], hide_index=True)
        st.write(f"미배정으로 보존할 문항: {len(plan.effects['detached'])}개 · ID 충돌 교체: {sum(len(v) for v in plan.effects['replace'].values())}개")
        if plan.already_matches:
            st.success("현재 대상 내용이 원본과 일치합니다.")
        for message in plan.effects["blockers"]:
            st.error(message)
    ready = len(plans) == len(selection) and not errors and all(not p.effects["blockers"] for p in plans.values())
    if st.button("선택 서버에 원본으로 덮어쓰기", type="primary", disabled=not ready):
        results = {}
        for label, plan in plans.items():
            with st.spinner(f"{label} 등록·검증 중"):
                try:
                    results[label] = execute_transfer(source, targets[label], plan)
                except TransferError as exc:
                    results[label] = {"status": "check_required", "message": str(exc)}
        st.session_state["bank-transfer-result"] = (request_key, results)
        st.session_state.pop("bank-transfer-preview", None)
        st.rerun()

result = st.session_state.get("bank-transfer-result")
if result and result[0] == request_key:
    for label, value in result[1].items():
        if value["status"] == "verified":
            st.success(f"{label}: 등록 내용 일치 확인 완료 · 문항 {value['counts']['items']}개 · 세트 {value['counts']['question_sets']}개")
        else:
            st.warning(f"{label}: {value['message']}")
    st.caption("다시 실행하려면 ‘등록 내용 확인’을 누르세요. 이미 일치하는 대상은 중복 등록하지 않습니다.")
