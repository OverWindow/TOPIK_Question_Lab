from topik_question_lab.common.paths import application_root
import os

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.common.question_bank.connection import TransferError, digest
from topik_question_lab.menus.korean_dictionary.korean_dictionary_source import LANGUAGES, TYPE_LABELS, capture_dictionary, dictionary_files
from topik_question_lab.menus.korean_dictionary.korean_dictionary_import import execute_dictionary_import, prepare_dictionary_import


ROOT = application_root()
load_dotenv(ROOT / ".env")
DIRECTORY = ROOT / "한국어기초사전"
st.set_page_config(page_title="한국어 사전 등록", page_icon="📘", layout="wide")
st.title("한국어 사전 등록")
st.caption("한국어기초사전 폴더의 전체 XLS/XLSX를 검증하고 선택한 서버에 등록합니다.")


def clear_preview():
    previous = st.session_state.pop("dictionary-preview", None)
    if previous:
        previous[1].close()


files = []
try:
    for path in dictionary_files(DIRECTORY):
        info = path.stat()
        files.append((path.name, info.st_size, info.st_mtime_ns))
except (OSError, TransferError) as exc:
    st.info(str(exc) if isinstance(exc, TransferError) else "사전 폴더의 파일 접근 권한을 확인하세요.")
if files:
    st.dataframe([{"원본 파일": name, "크기 (MB)": round(size / 1024**2, 1)} for name, size, _ in files],
                 hide_index=True, width="stretch")

targets = {
    "로컬 서버": os.getenv("QUESTION_BANK_LOCAL_DATABASE_URL", "").strip(),
    "운영 서버": os.getenv("QUESTION_BANK_PRODUCTION_DATABASE_URL", "").strip(),
}
selection = st.multiselect("등록 대상", list(targets), default=["로컬 서버"])
configured = bool(files and selection and all(targets[label] for label in selection))
if selection and not all(targets[label] for label in selection):
    st.info("선택 대상의 QUESTION_BANK_LOCAL_DATABASE_URL / QUESTION_BANK_PRODUCTION_DATABASE_URL을 설정하세요.")
if "운영 서버" in selection:
    st.warning("운영 서버가 선택되어 있습니다. 등록 버튼을 누르면 운영 DB에 적용됩니다.")
st.caption("같은 파일명·행 번호는 기존 ID를 유지해 갱신합니다. 다른 배포본이나 일부 파일만 등록하는 작업은 중단합니다.")
st.caption("기존 내용 변경 전 백업을 남기며, 서버마다 전체 성공 시 확정합니다. 테이블은 unigate-main의 기존 사전 테이블을 사용합니다.")

config_key = digest((targets, files))
if st.session_state.get("dictionary-config") != config_key:
    clear_preview()
    st.session_state["dictionary-config"] = config_key
    st.session_state.pop("dictionary-results", None)
request_key = digest((config_key, selection))
preview = st.session_state.get("dictionary-preview")
if preview and preview[0] != request_key:
    clear_preview()

progress = st.empty()
if st.button("등록 내용 확인", disabled=not configured):
    clear_preview()
    try:
        with st.spinner("원본 전체와 대상 사전을 확인합니다."):
            snapshot = capture_dictionary(DIRECTORY, progress=progress.caption)
            plans, errors, destinations = {}, {}, {}
            for label in selection:
                progress.caption(f"{label} 사전 확인 중")
                try:
                    plan = prepare_dictionary_import(snapshot, targets[label])
                    identity = plan.identity
                    destination = digest((identity["cluster"] or (identity["address"], identity["port"]), identity["database"]))
                    if destination in destinations:
                        errors[label] = f"{destinations[destination]}와 같은 DB입니다. 한 대상만 선택하세요."
                    else:
                        destinations[destination] = label
                        plans[label] = plan
                except TransferError as exc:
                    errors[label] = str(exc)
            st.session_state["dictionary-preview"] = (request_key, snapshot, plans, errors)
    except TransferError as exc:
        st.error(str(exc))
    finally:
        progress.empty()

preview = st.session_state.get("dictionary-preview")
preview_area = st.empty()
if preview and preview[0] == request_key:
    _, snapshot, plans, errors = preview
    with preview_area.container():
        st.subheader("원본 검증 결과")
        st.metric("전체 뜻풀이", f"{len(snapshot.hashes):,}")
        st.dataframe([{"원본 파일": name, "뜻풀이 수": count} for name, count in snapshot.counts.items()], hide_index=True)
        st.dataframe([{"구분": TYPE_LABELS[kind], "뜻풀이 수": snapshot.types[kind]} for kind in TYPE_LABELS], hide_index=True)
        with st.expander("언어별 번역 누락 및 원본 예시"):
            st.caption("번역 또는 번역 뜻풀이가 빈 항목도 원본 그대로 저장합니다.")
            st.dataframe([{"언어": name, "불완전한 번역 쌍": snapshot.missing[code]} for code, name in LANGUAGES.items()], hide_index=True)
            st.dataframe([{"표제어": row["headword"], "품사": row["part_of_speech"], "뜻풀이": row["definition"]}
                          for row in snapshot.sample], hide_index=True, width="stretch")
        for label, message in errors.items():
            st.error(f"{label}: {message}")
        for label, plan in plans.items():
            state = plan.state
            st.subheader(label)
            st.dataframe([{"현재 뜻풀이": state["count"], "신규": state["created"],
                           "변경": state["updated"], "동일": state["unchanged"]}], hide_index=True)
            if state["unchanged"] == len(snapshot.hashes):
                st.success("원본과 같습니다. 추가·갱신할 뜻풀이가 없습니다.")
        ready = configured and not errors and len(plans) == len(selection)
        execute = st.button("선택 서버에 사전 등록", type="primary", disabled=not ready)
    if execute:
        preview_area.empty()
        results = dict(st.session_state.get("dictionary-results", {}))
        for label, plan in plans.items():
            with st.spinner(f"{label} 사전 등록·검증 중"):
                try:
                    results[label] = execute_dictionary_import(snapshot, targets[label], plan, ROOT,
                                                                progress=progress.caption)
                except TransferError as exc:
                    results[label] = {"status": "check_required", "message": str(exc)}
            st.session_state["dictionary-results"] = results
        clear_preview()
        st.rerun()

results = st.session_state.get("dictionary-results")
if results:
    st.subheader("최근 서버별 실행 결과")
    for label, result in results.items():
        if result["status"] == "verified":
            st.success(f"{label}: {result['total']:,}개 검증 완료 · 신규 {result['created']:,} · "
                       f"변경 {result['updated']:,} · 동일 {result['unchanged']:,}")
            if result["backup"]:
                st.caption(f"변경 전 백업: {result['backup']}")
        else:
            st.warning(f"{label}: {result['message']}")
    st.caption("실패한 서버만 선택해 ‘등록 내용 확인’부터 재시도할 수 있습니다. 서버별 결과는 독립적으로 유지됩니다.")
