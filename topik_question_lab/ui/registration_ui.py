"""Shared Korean registration UI; all initial reads are side-effect free."""
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from topik_question_lab.step_06_publication.registration_workflow import RegistrationWorkflow, can_register
from topik_question_lab.step_06_publication.question_trash import QuestionTrash, trash_token
from topik_question_lab.step_06_publication.local_set_cleanup import LocalSetCleanup
from topik_question_lab.common.local_cleanup import cleanup_receipts
from topik_question_lab.step_06_publication.direct_publication import PublicationJobs, _hash
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog, default_primary_skill, display_stem, group_by_slot
from topik_question_lab.step_06_publication.storage_overview import load_overview, REVIEW_LABELS, visual_requirements, PHASE_LABELS
from topik_question_lab.step_06_publication.postgres_inspection import inspect_database
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank


SECTION = {"reading": "읽기", "listening": "듣기", "writing": "쓰기"}
MODES = {"add": "문제 추가하기", "set": "50문제 세트 만들기", "update": "변경 내용 반영",
         "review": "검수할 문제", "sets": "등록된 세트", "batches": "생성 묶음 등록·승인",
         "cleanup": "미등록 문제 정리", "trash": "휴지통", "set-cleanup": "등록 완료 세트 로컬 정리"}
STATUS = {"prepared": "등록 준비", "publishing": "결과 확인 필요", "outcome_unknown": "결과 확인 필요",
          "succeeded": "등록 완료", "failed": "재시도 가능", "stale": "다시 선택 필요", "conflict": "내용 비교 필요"}


def change_mode(mode):
    st.session_state["registration-mode"] = mode
    st.session_state.pop("registration-plan", None)
    st.session_state.pop("registration-result", None)
    st.session_state.pop("trash-result", None)
    st.session_state.pop("set-cleanup-plan", None)
    st.session_state.pop("set-cleanup-result", None)


def clear_snapshot():
    st.session_state.pop("set-cleanup-plan", None)
    st.session_state.pop("registration-snapshot", None)
    st.session_state.pop("registration-plan", None)
    st.session_state.pop("storage-overview", None)


def reload_page():
    clear_snapshot()
    st.rerun()


def question_label(c):
    return f"{SECTION.get(c.section, c.section)} {c.slot}번 · {c.model_id} · #{c.generated_id} · {display_stem(c)[:65]}"


def show_question(question):
    seen = set()
    for field in ("auxiliary_text", "passage", "stem", "question_prompt"):
        text = question.get(field)
        if text and text not in seen:
            st.write(text)
            seen.add(text)
    for turn in question.get("dialogue_turns") or []:
        st.write(f"{turn.get('speaker', '')}: {turn.get('text', '')}")
    for n, choice in enumerate(question.get("choices") or [], 1):
        st.write(f"{n}. {choice}")
    for option in question.get("visual_options") or []:
        st.write(f"{option.get('number')}. {option.get('description', '')}")
    st.write(f"정답: {question.get('answer', '미기록')}")
    st.caption(f"해설: {question.get('explanation') or '미기록'}")


def metadata_editor(candidates, remote, prefix, default_level=4):
    with st.expander("세부 설정 · 급수·난이도·출제 능력"):
        st.caption("기존 문제의 설정을 우선 사용합니다. 새 문제의 기본값은 TOPIK 4급, 예상 난이도 0.0입니다.")
        level = st.selectbox("새 문제·세트 기본 급수", [1, 2, 3, 4, 5, 6], index=default_level-1, key=f"{prefix}-level")
        difficulty = st.number_input("새 문제 기본 예상 난이도", -3.0, 3.0, 0.0, step=0.1, key=f"{prefix}-difficulty")
        rows = []
        for c in candidates:
            old = remote.get(c.source_key, {})
            old_difficulty = (old.get("source_provenance") or {}).get("publication_metadata", {}).get("predicted_difficulty", difficulty)
            rows.append({"key": c.source_key, "문항": f"{SECTION[c.section]} {c.slot}번 · #{c.generated_id}",
                         "급수": old.get("target_level", level), "난이도": float(old_difficulty),
                         "출제 능력": old.get("primary_skill", default_primary_skill(c))})
        if not rows:
            return {"level": level, "difficulty": difficulty, "items": {}}
        edited = st.data_editor(rows, hide_index=True, width="stretch", disabled=["key", "문항"],
            column_config={"key": None, "급수": st.column_config.NumberColumn(min_value=1, max_value=6, required=True),
                           "난이도": st.column_config.NumberColumn(min_value=-3.0, max_value=3.0, required=True),
                           "출제 능력": st.column_config.TextColumn(required=True)},
            key=f"{prefix}-metadata-{_hash([r['key'] for r in rows])}-{level}-{difficulty}")
        values = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
        return {"level": level, "difficulty": difficulty, "items": {
            r["key"]: {"target_level": int(r["급수"]), "predicted_difficulty": float(r["난이도"]), "primary_skill": r["출제 능력"]}
            for r in values}}


def filters(candidates, prefix):
    left, right = st.columns(2)
    section = left.selectbox("영역", ["all", "reading", "listening"],
                             format_func=lambda x: SECTION.get(x, "전체"), key=f"{prefix}-section")
    model = right.selectbox("생성 모델", ["all", *sorted({c.model_id for c in candidates})],
                            format_func=lambda x: "전체" if x == "all" else x, key=f"{prefix}-model")
    return [c for c in candidates if (section == "all" or c.section == section) and (model == "all" or c.model_id == model)]


def selection(snapshot, catalog, workflow, mode, ready):
    remote = {r["source_key"]: r for r in snapshot["bank"]["items"]}
    comparison = {r["source_key"]: r["status"] for r in snapshot["rows"]}
    selected, batch_id = [], None
    if mode in ("add", "update"):
        desired = "미등록" if mode == "add" else "내용 불일치"
        candidates = filters([r.candidate for r in catalog.records if can_register(r) and comparison.get(r.candidate.source_key) == desired], mode)
        st.caption("선택한 미등록 문제만 추가합니다." if mode == "add" else "선택한 문제의 변경 내용만 반영합니다. 변경 후 미디어 준비 상태도 확인하세요.")
        mapping = {c.source_key: c for c in candidates}
        keys = st.multiselect("등록할 문제" if mode == "add" else "변경할 문제", list(mapping),
                              format_func=lambda k: question_label(mapping[k]), key=f"{mode}-keys")
        selected = [mapping[k] for k in keys]
        if not candidates:
            st.info("조건에 맞는 승인·자동검사 통과 문항이 없습니다.")
        if mode == "update":
            blocked = sum(r["status"] == "내용 불일치" and not any(can_register(x) and x.candidate.source_key == r["source_key"] for x in catalog.records) for r in snapshot["rows"])
            if blocked:
                st.warning(f"내용이 다른 문제 중 {blocked}개는 검수·자동검사를 먼저 완료해야 합니다.")
    elif mode == "set":
        source = st.radio("문제 출처", ["기존 개별 생성 문제", "50문제 일괄 생성 묶음"], horizontal=True)
        if source == "50문제 일괄 생성 묶음":
            render_batches(snapshot, catalog, workflow, ready)
            return
        candidates = [r.candidate for r in catalog.records if can_register(r) and not r.candidate.batch_id and not remote.get(r.candidate.source_key, {}).get("set_id")]
        identities = sorted({(c.section, c.provider_key, c.model_id) for c in candidates})
        if not identities:
            st.info("세트에 사용하지 않은 승인 문항이 없습니다.")
            return
        identity = st.selectbox("세트 영역·모델", identities, format_func=lambda x: f"{SECTION[x[0]]} · {x[2]}")
        grouped = group_by_slot([c for c in candidates if (c.section, c.provider_key, c.model_id) == identity])
        missing = [n for n, values in grouped.items() if not values]
        if missing:
            st.warning(f"아직 {len(missing)}문제가 부족합니다. 필요한 번호: {', '.join(map(str, missing))}")
        for slot, values in grouped.items():
            if len(values) == 1:
                selected.append(values[0])
            elif values:
                choice = st.selectbox(f"{slot}번 후보 · {len(values)}개 중 선택", values, format_func=question_label,
                                      key=f"set-slot-{identity}-{slot}")
                selected.append(choice)
        st.caption("번호별 후보가 여러 개면 최신 후보를 먼저 선택합니다. 다른 세트에서 사용한 문제는 제외합니다.")
    if selected:
        st.dataframe([{"번호": c.slot, "영역": SECTION[c.section], "모델": c.model_id, "문제": display_stem(c)[:100]} for c in selected], hide_index=True, width="stretch")
    meta = metadata_editor(selected, remote, mode)
    def prepare_selection():
        try:
            st.session_state["registration-plan"] = workflow.prepare(mode, [c.source_key for c in selected], meta, batch_id=batch_id)
        except ValueError as exc:
            st.session_state["registration-error"] = str(exc)
        except Exception:
            st.session_state["registration-error"] = "선택 내용을 확인하지 못했습니다. 연결 상태를 확인하고 새로고침하세요."
    st.button("선택 내용 확인", type="primary", disabled=not ready or not selected or (mode == "set" and len(selected) != 50), on_click=prepare_selection)


def render_batches(snapshot, catalog, workflow, ready):
    batches = snapshot["batches"]
    if not batches:
        st.info("일괄 생성한 묶음이 없습니다. 50문제 일괄 생성에서 먼저 생성하세요.")
        return
    labels = {b["id"]: f"{SECTION[b['section']]} · {b['model']} · {b['created_at']} UTC" for b in batches}
    linked = st.session_state.pop("publication-batch-link", None)
    if linked in labels:
        st.session_state["registration-batch"] = linked
    bid = st.selectbox("생성 묶음", list(labels), format_func=labels.get, key="registration-batch")
    b = next(b for b in batches if b["id"] == bid)
    if any(bid in r["batch_ids"] for r in cleanup_receipts(workflow.root)):
        st.info("등록 완료 · 로컬 정리 이력 있음. 등록된 세트에서 확인하세요. 일부 정리는 로컬 정리 화면에서 이어서 진행할 수 있습니다.")
        st.button("로컬 정리 상태 보기", on_click=change_mode, args=("set-cleanup",))
        return
    records = [r for r in catalog.records if r.candidate.batch_id == bid]
    st.write(f"문제 {b['local_count']}/50 · 승인 {sum(r.candidate.review.get('approved', False) for r in records)}/50")
    if b["registered_count"] == 50:
        st.success(f"등록 완료 · {b['review_status']}")
    elif b["registered_count"] is None:
        st.warning("DB 등록 여부를 확인할 수 없습니다.")
    else:
        st.info(f"DB 등록 {b['registered_count']}/50")
    coverage = b["coverage"]
    if coverage:
        show_coverage(coverage)
    p = b["preparation"]
    if b.get("pipeline_status") and b["registered_count"] != 50:
        st.warning(f"미디어·자동 등록 단계: {PHASE_LABELS.get(b['pipeline_status'], b['pipeline_status'])} · 음원 준비 {p['audio_bound'] if p['audio_bound'] is not None else '확인 필요'}/{'50' if b['section']=='listening' else '0'} · 이미지 준비 {p['images_ready'] if p['images_ready'] is not None else '확인 필요'}/{p['images_required'] if p['images_required'] is not None else '확인 필요'}")
        if st.button("미디어 완성·재시도 화면으로 이동"):
            st.switch_page(str(Path(__file__).parent / "bulk_generation_app.py"))
        return
    if b["registered_count"] == 50 and b["review_status"] != "검수 대기":
        st.caption("이미 등록된 세트입니다. 문제 저장 현황에서 내용을 확인하세요.")
        return
    candidates = [r.candidate for r in records]
    remote = {r["source_key"]: r for r in snapshot["bank"]["items"]}
    stored_set = next((s for s in snapshot["bank"]["sets"] if str(s["set_id"]) == b["set_id"]), {})
    meta = metadata_editor(candidates, remote, f"batch-{bid}", stored_set.get("default_target_level", 4))
    valid = len(records) == 50 and all(can_register(r) for r in records) and b["status"] == "succeeded"
    if not valid:
        st.warning("50문항의 검수 승인과 자동검사를 모두 완료해야 합니다.")
    def prepare_batch():
        try:
            st.session_state["registration-plan"] = workflow.prepare("batch", [c.source_key for c in candidates], meta, batch_id=bid)
        except ValueError as exc:
            st.session_state["registration-error"] = str(exc)
        except Exception:
            st.session_state["registration-error"] = "생성 묶음을 확인하지 못했습니다. 새로고침 후 다시 확인하세요."
    st.button("생성 묶음 내용 확인", type="primary", disabled=not ready or not valid, on_click=prepare_batch)


def confirmation(plan, workflow):
    st.subheader("② 내용 확인")
    candidates = plan["candidates"]
    baseline = plan["baseline"]
    summary = plan["summary"]
    overwrite_confirmed = True
    if plan["mode"] == "update" and any(r and r["source_provenance"].get("bank_edited_at") for r in baseline.values()):
        st.warning("PostgreSQL 문제 관리에서 편집한 문항이 포함되어 있습니다. 실행하면 해당 내용을 아래 SQLite 내용으로 덮어씁니다.")
        overwrite_confirmed = st.checkbox("PostgreSQL 편집본을 SQLite 내용으로 덮어쓰는 것을 확인했습니다")
    st.write(f"선택 {len(candidates)}문제 · 신규 {summary['new']} · 변경 {summary['changed']} · 변경 없음 {summary['unchanged']} · 새 세트 {summary['sets']}개")
    st.caption("기존 문제의 내용·설정 변경은 아래 비교를 확인하세요. 실행 직전에 상태를 다시 검사합니다.")
    idx = st.selectbox("미리 볼 문제", range(len(candidates)), format_func=lambda i: f"{SECTION[candidates[i]['section']]} {candidates[i]['question'].get('type_slot')}번 · #{candidates[i]['generated_id']}")
    c = candidates[idx]
    key = f"{c['section']}:{c['question_type']}:{c['generated_id']}"
    left, right = st.columns(2)
    with left:
        st.markdown("**등록할 내용 · SQLite**")
        show_question(c["question"])
    with right:
        st.markdown("**현재 등록 내용 · PostgreSQL**")
        if baseline[key]:
            show_question(baseline[key]["content_json"])
        else:
            st.info("새로 등록할 문제입니다.")
    if plan["mode"] == "batch" and any(baseline.values()):
        label = "선택 세트 승인 상태 반영"
    elif plan["mode"] in ("set", "batch"):
        label = f"{SECTION[candidates[0]['section']]} 50문제 세트 등록하기"
    elif plan["mode"] == "update":
        label = f"선택한 {len(candidates)}문제 변경 내용 반영하기"
    else:
        label = f"선택한 {len(candidates)}문제 등록하기"
    st.info("미디어 생성은 실행하지 않습니다. 등록 후 미디어 준비 상태를 별도로 확인하세요.")
    a, b = st.columns(2)
    a.button("대상 다시 선택", on_click=lambda: st.session_state.pop("registration-plan", None))
    def register():
        try:
            result = workflow.execute(plan)
            st.session_state["registration-result"] = result
            for key in ("registration-snapshot", "registration-plan", "storage-overview"):
                st.session_state.pop(key, None)
        except ValueError as exc:
            st.session_state["registration-error"] = str(exc)
        except Exception:
            st.session_state["registration-error"] = "등록 결과 확인이 필요합니다. 아래 최근 등록 결과에서 다시 확인하세요."
    b.button(label, type="primary", on_click=register, disabled=not overwrite_confirmed)


def show_coverage(coverage):
    if coverage is None:
        st.warning("미디어 연결: 확인 불가")
        return
    c = coverage
    if c["audio_required"]:
        st.write(f"{'✅' if c['audio_bound']==c['audio_required'] else '⚠️'} 음원 연결 {c['audio_bound']}/{c['audio_required']}문항 · 파일 {c['audio_files']}개")
    else:
        st.caption("읽기 문항은 음원이 필요하지 않습니다.")
    st.write(f"{'✅' if c['images_ready']==c['images_required'] else '⚠️'} 이미지 연결 {c['images_ready']}/{c['images_required']}")
    if c["audio_bound"] != c["audio_required"] or c["images_ready"] != c["images_required"]:
        st.warning("문항 등록 완료 · 미디어 준비 필요")


def render_sets(snapshot, url):
    sets = snapshot["bank"]["sets"]
    if not sets:
        st.info("조회된 세트가 없습니다." if snapshot["bank"]["available"] else "DB 연결 상태를 확인하세요.")
        return
    label = lambda s: f"{SECTION[s['section']]} · {s['generator_version']} · {s['set_sequence']}번 세트"
    chosen = st.selectbox("확인할 세트", range(len(sets)), format_func=lambda i: label(sets[i]))
    s = sets[chosen]
    with st.container(border=True):
        st.subheader(label(s))
        st.write(f"{'✅' if s['item_count']==50 else '⚠️'} 문제 {s['item_count']}/50 · 등록 완료 · {REVIEW_LABELS.get(s['review_status'],s['review_status'])}")
        show_coverage(s["coverage"])
        key = f"registration-verification-{s['set_id']}"
        st.caption("URL 연결 여부와 실제 파일 검증 결과는 별개입니다.")
        if st.button("음원·이미지 실제 파일 확인"):
            from topik_question_lab.step_06_publication.media_cloud import preview_s3_media
            try:
                with st.spinner("S3·CloudFront 파일을 확인하고 있습니다."):
                    result = preview_s3_media(url, [str(s["set_id"])])
                message = f"✅ 실제 파일 검증 통과 · {result['files']}파일"
            except Exception:
                message = "⚠️ 실제 파일 검증 실패 · 필수 미디어 연결과 S3 접근 설정을 확인하세요."
            st.session_state[key] = (snapshot["checked_at"], message, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        checked = st.session_state.get(key)
        if checked and checked[0] == snapshot["checked_at"]:
            st.write(f"{checked[1]} · {checked[2]}")
        else:
            st.caption("실제 파일 접근: 미검증")
        members = sorted([i for i in snapshot["bank"]["items"] if i["set_id"] == s["set_id"]], key=lambda i:i["position"])
        from topik_question_lab.step_06_publication.bank_editor import set_health
        st.caption(set_health(members)["label"])
        if snapshot["bank"]["media_available"]:
            missing_audio = [i["position"] for i in members if i["section"] == "listening" and (
                not i.get("audio") or not i["audio"]["storage_url"] or i["audio"]["narration_version"] != "exam_track_v4")]
            missing_images = [i["position"] for i in members if visual_requirements(i["section"], i["type_slot"], i["content_json"]) -
                              {(v["visual_role"], v["option_number"]) for v in i.get("visuals", []) if v["storage_url"]}]
            if missing_audio:
                st.warning(f"음원 연결이 필요한 번호: {', '.join(map(str, missing_audio))}")
            if missing_images:
                st.warning(f"필수 이미지가 부족한 번호: {', '.join(map(str, missing_images))}")
        with st.expander("문제 보기"):
            if members:
                n = st.selectbox("문항 번호", range(len(members)), format_func=lambda i: str(members[i]["position"]))
                show_question(members[n]["content_json"])
                if st.button("선택 문항 PostgreSQL에서 편집"):
                    st.session_state["bank-editor-open-item"] = str(members[n]["item_id"])
                    st.switch_page(str(Path(__file__).with_name("bank_editor_app.py")))
        with st.expander("고급 검증 · 로컬 파일"):
            st.caption("로컬 파일 저장소를 사용하는 기존 세트용입니다.")
            if st.button("로컬 문항·미디어 검증"):
                import os
                from topik_question_lab.step_06_publication.media_validation import preview_local_media
                try:
                    root = Path(os.getenv("TOPIK_LAB_ROOT", str(Path(__file__).resolve().parents[2])))
                    result = preview_local_media(url, [str(s["set_id"])], Path(os.getenv("LOCAL_MEDIA_ROOT", str(root / "data/media"))))
                    st.success(f"로컬 파일 검증 통과 · {result['files']}파일")
                except Exception:
                    st.warning("로컬 파일 검증 실패 · 저장소 설정과 파일·해시를 확인하세요.")


def render_history(root, workflow, ready):
    st.subheader("최근 등록 결과")
    try:
        jobs = PublicationJobs(root, readonly=True).list()
    except Exception:
        st.warning("등록 이력을 읽지 못했습니다.")
        return
    if not jobs:
        st.caption("등록 작업 이력이 없습니다.")
    for job in jobs[:20]:
        current = job["target"] == workflow.target
        with st.expander(f"{job['created_at']} UTC · {STATUS.get(job['status'],job['status'])}" + ("" if current else " · 이전 DB · 조회 전용")):
            packages = job["payload"]["packages"]
            st.write(f"대상 {sum(len(p.get('candidates', [])) for p in packages)}문제")
            st.caption(f"작업 ID: {job['id']}")
            if job["status"] != "succeeded":
                st.info("문제나 DB 내용이 변경된 작업은 다시 선택해야 합니다. 미디어 작업은 일괄 생성 화면에서 재시도하세요.")
            if st.button("등록 결과 다시 확인", key=f"registration-check-{job['id']}", disabled=not ready or not current):
                try:
                    st.session_state["registration-result"] = workflow.reconcile(job["id"])
                    reload_page()
                except Exception:
                    st.error("결과를 확인하지 못했습니다. DB 연결 상태를 확인하세요.")
            if current and job["status"] in {"failed", "publishing", "outcome_unknown"} and not any("media" in p for p in packages):
                if st.button("같은 작업 재시도", key=f"registration-retry-{job['id']}", disabled=not ready):
                    try:
                        st.session_state["registration-result"] = workflow.retry(job["id"])
                        reload_page()
                    except Exception:
                        st.error("재시도 결과를 확인하지 못했습니다. 등록 결과를 다시 확인하세요.")


def render_trash(root, url, snapshot, catalog, trashed, ready, *, restore):
    remote = {r["source_key"] for r in snapshot["bank"]["items"]}
    records = trashed if restore else [r for r in catalog.records if r.candidate.source_key not in remote and not r.candidate.batch_id]
    st.caption("문제 내용·기존 승인 상태를 그대로 복원합니다. PostgreSQL에 등록하는 작업은 아닙니다." if restore else
               "승인 여부와 관계없이 미등록 개별 문제를 정리할 수 있습니다. 문제 내용과 검수 기록은 보존되며 언제든 복원할 수 있습니다.")
    if not restore:
        st.caption("일괄 생성 묶음에 속한 문제는 연결 기록을 보호하기 위해 개별 처리를 제한합니다.")
    if not ready:
        st.warning("PostgreSQL 등록 여부와 로컬 기록을 확인할 수 있어야 휴지통 이동·복원을 실행할 수 있습니다.")
    candidates = filters([r.candidate for r in records], "trash-restore" if restore else "trash-cleanup")
    by_key = {r.candidate.source_key: r for r in records}
    visible = {c.source_key: c for c in candidates}
    if not visible:
        st.info("휴지통이 비어 있거나 필터에 맞는 문제가 없습니다." if restore else "정리할 미등록 개별 문제가 없습니다.")
        return
    selection_key = "trash-restore-keys" if restore else "trash-cleanup-keys"
    previous_selection = st.session_state.get(selection_key, [])
    if any(key not in visible for key in previous_selection):
        st.session_state[selection_key] = [key for key in previous_selection if key in visible]
    st.button("현재 목록 전체 선택", key=f"{selection_key}-all", disabled=not ready,
              on_click=lambda: st.session_state.update({selection_key: list(visible)}))
    selected = st.multiselect("복원할 문제" if restore else "버릴 문제", list(visible),
                              format_func=lambda k: question_label(visible[k]), key=selection_key)
    st.dataframe([{"문제": question_label(c), "검수": "승인" if c.review.get("approved") else "검수 대기",
                   "휴지통 이동 시각 (UTC)": by_key[c.source_key].trashed_at or "—"} for c in candidates],
                 hide_index=True, width="stretch")
    if selected:
        with st.expander("선택한 문제 내용 확인"):
            key = st.selectbox("미리 볼 문제", selected, format_func=lambda k: question_label(visible[k]), key=f"{selection_key}-preview")
            show_question(visible[key].question)
    expected = {key: trash_token(by_key[key]) for key in selected}

    def apply_selection():
        try:
            result = QuestionTrash(root, url).apply(expected, restore=restore)
            st.session_state["trash-result"] = result
            for key in ("registration-snapshot", "registration-plan", "registration-result", "storage-overview", selection_key):
                st.session_state.pop(key, None)
        except ValueError as exc:
            st.session_state["registration-error"] = str(exc)
        except Exception:
            st.session_state["registration-error"] = "휴지통 상태를 확인하지 못했습니다. 새로고침 후 다시 확인하세요."
    label = f"선택한 {len(selected)}문제 복원하기" if restore else f"선택한 {len(selected)}문제 휴지통으로 이동"
    st.button(label, type="primary", disabled=not ready or not selected, on_click=apply_selection)


def render_set_cleanup(root, url, snapshot):
    service = LocalSetCleanup(root, url)
    st.caption("승인과 필수 미디어 S3 등록을 마친 세트의 로컬 문제·검수 기록을 백업 후 삭제합니다. 생성·등록 이력과 PostgreSQL·S3 파일은 유지합니다.")
    st.caption("미디어 등록 정보와 URL 연결을 확인합니다. 실제 파일 접근 검사는 등록된 세트의 검증 버튼에서 별도로 실행할 수 있습니다.")
    try:
        rows = service.candidates(snapshot)
    except ValueError as exc:
        st.warning(str(exc))
        return
    except Exception:
        st.warning("정리 이력을 읽지 못했습니다. 로컬 DB를 확인하세요.")
        return
    label = lambda r: f"{SECTION[r['section']]} · {r['model']} · {r['sequence']}번 세트"
    st.dataframe([{"세트": label(r), "남은 로컬 문제": r["count"],
                   "상태": "로컬 정리 완료" if r["complete"] else " · ".join(r["reasons"]) or "정리 가능"}
                  for r in rows], hide_index=True, width="stretch")
    plan = st.session_state.get("set-cleanup-plan")
    if plan:
        count = sum(r["count"] for r in plan["sets"])
        st.subheader("정리 내용 확인")
        st.write(f"선택한 {len(plan['sets'])}세트의 로컬 문제 {count}개와 해당 검수 기록을 삭제합니다.")
        st.write("현재 PostgreSQL 내용을 기준으로 유지합니다. 로컬 내용이 다르더라도 백업에 보관한 뒤 삭제합니다. 복구는 백업 복원으로 진행합니다.")
        st.dataframe([{"세트": label(r), "삭제할 로컬 문제": r["count"]} for r in plan["sets"]], hide_index=True)
        confirmed = st.checkbox("백업 후 선택한 로컬 문제와 검수 기록을 삭제합니다", key="set-cleanup-confirm")
        def execute():
            try:
                st.session_state["set-cleanup-result"] = service.execute(plan)
                clear_snapshot()
            except ValueError as exc:
                st.session_state["registration-error"] = str(exc)
                st.session_state.pop("set-cleanup-plan", None)
        st.button(f"백업 후 {count}문제 로컬 정리하기", type="primary", disabled=not confirmed, on_click=execute)
        st.button("다시 선택", on_click=lambda: st.session_state.pop("set-cleanup-plan", None))
        return
    eligible = {r["set_id"]: r for r in rows if r["count"] and not r["reasons"]}
    chosen = st.multiselect("로컬 정리할 세트", list(eligible), format_func=lambda k: label(eligible[k]))
    def prepare():
        try:
            st.session_state["set-cleanup-plan"] = service.prepare(chosen)
            st.session_state["set-cleanup-confirm"] = False
        except ValueError as exc:
            st.session_state["registration-error"] = str(exc)
    st.button("선택 세트 정리 내용 확인", disabled=not chosen, on_click=prepare)


def render_app(root, url, initial_mode="add"):
    st.title("문제 등록·세트 만들기")
    st.caption("대상을 선택하고 내용을 확인하면 등록까지 한 번에 처리합니다.")
    try:
        workflow = RegistrationWorkflow(root, url)
    except ValueError:
        st.error("DB 연결 설정 형식을 확인하세요.")
        return
    identity = (str(root), workflow.target)
    saved = st.session_state.get("registration-snapshot")
    if not saved or saved["identity"] != identity or "trashed" not in saved:
        st.session_state.pop("registration-plan", None)
        if saved and saved["identity"] != identity:
            st.session_state.pop("registration-result", None)
        with st.spinner("문제와 등록 상태를 읽고 있습니다."):
            snapshot = load_overview(root, url)
            catalog = collect_candidate_catalog(root)
            all_catalog = collect_candidate_catalog(root, include_trashed=True)
            trashed = [r for r in all_catalog.records if r.trashed_at is not None]
            snapshot["errors"] = list(dict.fromkeys([*snapshot["errors"], *all_catalog.read_errors]))
            schema_ready = inspect_database(url).ready if url else False
        saved = {"identity": identity, "snapshot": snapshot, "catalog": catalog, "schema_ready": schema_ready, "trashed": trashed}
        st.session_state["registration-snapshot"] = saved
    snapshot, catalog = saved["snapshot"], saved["catalog"]
    bank = snapshot["bank"]
    ready = bank["available"] and saved["schema_ready"] and not snapshot["errors"]
    st.button("현황 새로고침", on_click=clear_snapshot)
    st.caption(f"조회 시각 {snapshot['checked_at']} · UTC")
    if not url:
        st.info("로컬 PostgreSQL 연결이 필요합니다. 고급 설정에서 연결 준비 상태를 확인하세요.")
    elif not bank["available"]:
        st.warning(bank["message"])
    elif not saved["schema_ready"]:
        st.warning("DB 구조 확인이 필요합니다. 고급 설정에서 준비 상태를 확인하세요.")
    for error in snapshot["errors"]:
        st.warning(error)
    error = st.session_state.pop("registration-error", None)
    if error:
        st.error(error)
    eligible = {r.candidate.source_key for r in catalog.records if can_register(r)}
    counts = [sum(r["status"] == "미등록" and r["source_key"] in eligible for r in snapshot["rows"]) if ready else "확인 불가",
              sum(not r.candidate.review.get("approved") for r in catalog.records) if not snapshot["errors"] else "확인 불가",
              sum(r["status"] == "내용 불일치" for r in snapshot["rows"]) if ready else "확인 불가",
              len(bank["sets"]) if bank["available"] else "확인 불가"]
    for col, title, count, mode in zip(st.columns(4), ["바로 등록 가능", "검수 필요", "저장 내용 비교 필요", "등록된 세트"], counts, ["add", "review", "update", "sets"]):
        col.metric(title, count)
        col.button(f"{title} 보기", key=f"registration-summary-{mode}", width="stretch", on_click=change_mode, args=(mode,))
    if bank["available"]:
        st.caption(f"등록된 세트: 읽기 {sum(s['section']=='reading' for s in bank['sets'])} · 듣기 {sum(s['section']=='listening' for s in bank['sets'])}")
    errors = sum(any(v.get("severity", "error") == "error" for v in r.candidate.validation) for r in catalog.records)
    if errors:
        st.warning(f"자동검사 오류가 있는 문제 {errors}개는 수정 후 등록할 수 있습니다.")
    left, right = st.columns(2)
    left.button("문제 추가하기", width="stretch", type="primary", on_click=change_mode, args=("add",))
    right.button("50문제 세트 만들기", width="stretch", type="primary", on_click=change_mode, args=("set",))
    clean, trash = st.columns(2)
    clean.button("미등록 문제 정리", width="stretch", on_click=change_mode, args=("cleanup",))
    trash.button(f"휴지통 보기 · {len(saved['trashed'])}문제", width="stretch", on_click=change_mode, args=("trash",))
    st.button("등록 완료 세트 로컬 정리", on_click=change_mode, args=("set-cleanup",))
    cleanup_result = st.session_state.get("set-cleanup-result")
    if cleanup_result:
        count = len(cleanup_result["changed"])
        if cleanup_result["complete"]:
            st.success(f"로컬 {count}문제 정리 완료 · PostgreSQL과 S3는 변경하지 않았습니다.")
        else:
            st.warning(f"로컬 {count}문제 정리 확인 · 일부 파일은 완료하지 못했습니다. 새로고침 후 남은 대상을 다시 선택하세요.")
        st.code(cleanup_result["backup"], language=None)
    trash_result = st.session_state.get("trash-result")
    if trash_result:
        count = len(trash_result["changed"])
        action = "복원" if trash_result["restore"] else "휴지통으로 이동"
        if trash_result["complete"]:
            st.success(f"{count}문제를 {action}했습니다. PostgreSQL 문항은 변경하지 않았습니다.")
        else:
            st.warning(f"{count}문제 처리 확인 · {trash_result['message']}")
    if "publication-batch-link" in st.session_state:
        st.session_state["registration-mode"] = "batches"
    mode = st.session_state.setdefault("registration-mode", initial_mode)
    result = st.session_state.get("registration-result")
    if result:
        if result["status"] == "succeeded":
            st.success("③ 등록 완료 · 문항 반영을 확인했습니다. 세트·미디어 준비 상태는 등록된 세트에서 확인하세요.")
        else:
            st.warning(f"{STATUS.get(result['status'], result['status'])} · {result.get('message') or '최근 등록 결과에서 다시 확인하세요.'}")
    st.divider()
    plan = st.session_state.get("registration-plan")
    if plan:
        confirmation(plan, workflow)
    else:
        st.subheader(MODES[mode])
        if mode in ("add", "update", "set"):
            st.caption("① 대상 선택 → ② 내용 확인 → ③ 등록 완료")
            selection(snapshot, catalog, workflow, mode, ready)
        elif mode == "sets":
            render_sets(snapshot, url)
        elif mode == "batches":
            render_batches(snapshot, catalog, workflow, ready)
        elif mode in {"cleanup", "trash"}:
            render_trash(root, url, snapshot, catalog, saved["trashed"], ready, restore=mode == "trash")
        elif mode == "set-cleanup":
            render_set_cleanup(root, url, snapshot)
        elif mode == "review":
            values = filters([r.candidate for r in catalog.records if not can_register(r)], "review")
            if values:
                selected = st.selectbox("검수할 문제", values, format_func=question_label)
                show_question(selected.question)
                if st.button("이 문제 검수하러 가기"):
                    st.session_state["registration-review-link"] = {"section": selected.section, "type_id": selected.question_type, "id": selected.generated_id}
                    name = "app.py" if selected.section == "reading" else "listening_app.py"
                    st.switch_page(str(Path(__file__).parent / name))
            else:
                st.info("검수가 필요한 문제가 없습니다.")
    with st.expander("자동 등록·생성 묶음 확인", expanded=mode == "batches"):
        st.caption(f"생성 묶음 {len(snapshot['batches'])}개 · 등록과 검수 상태를 확인하거나 승인 상태를 반영합니다.")
        st.button("생성 묶음 등록·승인 보기", on_click=change_mode, args=("batches",))
    render_history(root, workflow, ready)
    with st.expander("고급 설정 · DB 연결·구조 관리"):
        st.caption("일상적인 등록에는 이 설정을 실행할 필요가 없습니다. 기존 DB 구조 변경 전에는 전체 백업이 필요합니다.")
        if snapshot["rows"]:
            import csv
            import io
            fields = ["source_key", "section", "model_id", "type_slot", "status", "local_review", "postgres_review", "set_id"]
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: r.get(k) for k in fields} for r in snapshot["rows"])
            st.download_button("저장 상태 CSV 다운로드", output.getvalue().encode("utf-8-sig"), "question_storage_status.csv", "text/csv")
        if st.button("로컬 DB 연결·구조 확인", disabled=not url):
            status = inspect_database(url)
            (st.success if status.ready else st.warning)("연결·구조 확인 완료" if status.ready else "DB 연결·스키마를 확인하세요.")
        backup = st.checkbox("필요한 백업을 완료했고 DB 구조 변경을 실행합니다")
        if st.button("DB 구조 준비", disabled=not url or not backup):
            try:
                PostgresQuestionBank(url).ensure_schema()
                reload_page()
            except Exception:
                st.error("DB 구조 준비에 실패했습니다. 연결·권한·호환성을 확인하세요.")
        st.subheader("과거 배포 이력")
        if st.button("보관된 배포 이력 조회", disabled=not ready):
            try:
                st.session_state["registration-deployments"] = (identity, PostgresQuestionBank(url).list_deployment_runs())
            except Exception:
                st.warning("배포 이력을 조회하지 못했습니다.")
        history = st.session_state.get("registration-deployments")
        if history and history[0] == identity:
            # Omit old connection labels and raw exception messages.
            rows = [{"작업 ID": str(r["run_id"]), "상태": r["status"], "요청 세트": r["requested_set_count"],
                     "시작 시각": str(r["started_at"])} for r in history[1]]
            st.dataframe(rows, hide_index=True, width="stretch")
            if rows:
                import csv
                import io
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
                st.download_button("배포 이력 CSV 다운로드", output.getvalue().encode("utf-8-sig"), "deployment_history.csv", "text/csv")
    if st.button("문제 저장 현황 열기"):
        st.switch_page(str(Path(__file__).parent / "storage_overview_app.py"))
