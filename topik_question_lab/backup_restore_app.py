from __future__ import annotations

from pathlib import Path

import streamlit as st

from topik_question_lab.backup_service import (
    BackupOptions,
    create_backup,
    inspect_backup,
    manifest_summary,
    restore_backup,
    save_local_backup,
)


ROOT = Path(__file__).resolve().parents[1]

st.set_page_config(page_title="TOPIK Lab 백업·복원", page_icon="💾", layout="wide")
st.title("백업·복원")
st.caption("읽기와 듣기의 인식 자료, 유형 DB, 생성·검수 기록과 시각 자산을 하나의 검증 가능한 ZIP으로 관리합니다.")

if "backup_notice" in st.session_state:
    st.success(st.session_state.pop("backup_notice"))

reading_databases = list((ROOT / "data" / "types").glob("*.db"))
listening_databases = list((ROOT / "data" / "listening" / "types").glob("*.db"))
recognized_reading = list((ROOT / "extracted_text").glob("**/*")) if (ROOT / "extracted_text").exists() else []
recognized_listening = list((ROOT / "data" / "listening" / "imports").glob("*/questions.json"))

metrics = st.columns(4)
metrics[0].metric("읽기 유형 DB", len(reading_databases))
metrics[1].metric("듣기 유형 DB", len(listening_databases))
metrics[2].metric("읽기 인식 파일", sum(path.is_file() for path in recognized_reading))
metrics[3].metric("듣기 인식 회차", len(recognized_listening))

st.subheader("백업 만들기")
include_sources = st.checkbox(
    "원본 PDF·HTML 포함",
    value=True,
    help="읽기·듣기 시험지와 답안 PDF, 변환 HTML을 ZIP에 포함합니다.",
)
include_pages = st.checkbox(
    "렌더링된 듣기 페이지 PNG 포함",
    value=False,
    help="용량이 크게 증가합니다. 원본 PDF가 포함되면 복원 후 다시 렌더링할 수 있습니다.",
)
if st.button("읽기·듣기 전체 백업 생성", type="primary"):
    try:
        with st.spinner("일관된 DB 스냅샷과 백업 ZIP을 만들고 있습니다."):
            artifact = create_backup(ROOT, BackupOptions(include_sources=include_sources, include_rendered_pages=include_pages))
            local_path = save_local_backup(ROOT, artifact)
        st.session_state["backup_artifact"] = artifact.data
        st.session_state["backup_filename"] = artifact.filename
        st.session_state["backup_manifest"] = artifact.manifest
        st.session_state["backup_local_path"] = str(local_path)
        st.success(f"백업을 만들었습니다: {local_path}")
    except Exception as exc:
        st.error(str(exc))

if st.session_state.get("backup_artifact"):
    manifest = st.session_state["backup_manifest"]
    summary = manifest_summary(manifest)
    st.write(
        f"생성 시각: `{manifest['created_at']}` · 파일 {len(manifest['files'])}개 · "
        f"로컬 사본: `{st.session_state['backup_local_path']}`"
    )
    st.json(summary)
    st.download_button(
        "백업 ZIP 다운로드",
        st.session_state["backup_artifact"],
        st.session_state["backup_filename"],
        "application/zip",
        use_container_width=True,
    )

st.divider()
st.subheader("백업 다시 불러오기")
st.warning("복원은 백업에 포함된 파일만 덮어쓰며 다른 파일은 삭제하지 않습니다. 덮어쓰기 직전에 현재 상태의 안전 백업을 자동 생성합니다.")
uploaded = st.file_uploader("TOPIK Question Lab 백업 ZIP", type=["zip"])
restore_manifest = None
if uploaded:
    try:
        restore_manifest = inspect_backup(uploaded.getvalue())
        summary = manifest_summary(restore_manifest)
        st.success("백업 형식과 SHA-256 무결성 검사를 통과했습니다.")
        st.write(f"백업 시각: `{restore_manifest['created_at']}` · 파일 {len(restore_manifest['files'])}개")
        st.json(summary)
    except Exception as exc:
        st.error(str(exc))

confirm = st.checkbox("현재 파일을 백업 내용으로 덮어쓰는 것을 확인했습니다.", disabled=restore_manifest is None)
confirm_text = st.text_input("확인을 위해 '복원' 입력", disabled=restore_manifest is None)
restore_enabled = restore_manifest is not None and confirm and confirm_text.strip() == "복원"
if st.button("검증된 백업 복원", type="primary", disabled=not restore_enabled):
    try:
        with st.spinner("현재 상태를 안전 백업한 뒤 파일을 복원하고 있습니다."):
            restored_manifest, safety_path = restore_backup(ROOT, uploaded.getvalue())
        st.cache_resource.clear()
        st.cache_data.clear()
        st.session_state["backup_notice"] = (
            f"{len(restored_manifest['files'])}개 파일을 복원했습니다. "
            f"복원 전 안전 백업: {safety_path}"
        )
        st.rerun()
    except Exception as exc:
        st.error(str(exc))

