from __future__ import annotations

from topik_question_lab.common.paths import application_root
from topik_question_lab.common.ui.navigation import menu_page

from topik_question_lab.common.generation.exam_profiles import question_count, MAPPING_PATH
import json
import os
import time

import streamlit as st
from dotenv import load_dotenv

ROOT = application_root()
load_dotenv(ROOT / ".env")

from topik_question_lab.common.generation.bulk_generation import custom_model, model_options, model_capabilities_for, prepare_batch, profiles_for
from topik_question_lab.common.media.media_generation import preflight
from topik_question_lab.common.media.media_provider import MediaSettings
from topik_question_lab.common.generation.runner_service import get_runner
from topik_question_lab.menus.bulk_generation.bulk_progress import batch_review_details, completed_batches, needs_progress_refresh
from topik_question_lab.common.ui.set_identity import set_view, set_label, render_set_summary
from topik_question_lab.common.question_bank.storage_overview import load_overview
from topik_question_lab.common.generation.providers import DEFAULT_GENERATION_PRESET, GENERATION_PRESETS, can_gateway_call, generation_parameter_preview, has_chatkhu_api_key, list_chatkhu_models, provider_backend, provider_label, supports_generation_presets, BIZROUTER_PREFIX, SERVICE_LABELS, parse_bizrouter_models
from topik_question_lab.common.ui.provider_ui import generation_service, render_bizrouter_sync


def model_label(value):
    provider, model = value
    if provider_backend(provider, model) == "bizrouter":
        return provider_label(provider)
    backend = SERVICE_LABELS[provider_backend(provider, model)]
    return f"{provider_label(provider)} · {model} · {backend}"


st.set_page_config(page_title="문제 일괄 생성", page_icon="✨", layout="wide")
st.title("문제 일괄 생성")
exam_format = st.radio("시험 형식", ["pbt", "ibt"], format_func=str.upper, horizontal=True, key="bulk-exam-format")
expected_count = question_count(exam_format)
section = st.radio("영역", ["listening", "reading"], format_func=lambda value: "듣기" if value == "listening" else "읽기",
                   horizontal=True, key="bulk-section")
st.caption("문항 생성 → 음원·이미지 준비 → 세트 자동 등록까지 한 번에 진행합니다.")
runner = get_runner(str(ROOT))
runner.recover_idle()
store = runner.store
settings = store.settings()
model_area = st.container()
advanced = st.expander("고급 설정", expanded=False)
with advanced:
    service = generation_service(store, settings.get("generation_service", "all"), key="bulk-generation-service")
    auto_media = st.checkbox("음원·이미지 생성 및 DB 자동 등록", value=settings.get("auto-media", "true") == "true")
    store.set_setting("auto-media", "true" if auto_media else "false")
    tts_delay = st.number_input("TTS 요청 완료 후 대기 (초)", min_value=0.0, value=float(settings.get("tts-delay", 2)), step=0.5)
    image_delay = st.number_input("이미지 요청 완료 후 대기 (초)", min_value=0.0, value=float(settings.get("image-delay", 5)), step=0.5)
    st.caption(f"새 작업 이미지: BizRouter · {MediaSettings.from_env().image_model}")
    speaking_rate = st.slider("낭독 속도", 0.8, 1.2, 1.0, 0.025)
    style_prompt = st.text_input("음성 전달 스타일", placeholder="예: 차분하고 또렷하게")
    st.caption("일시적 오류는 최대 5회 자동 재시도합니다. 성공한 파일은 보존합니다.")
store.set_setting("tts-delay", str(tts_delay))
store.set_setting("image-delay", str(image_delay))


def media_config(section, exam_format):
    return preflight(ROOT, MediaSettings.from_env(tts_delay=tts_delay, image_delay=image_delay,
        speaking_rate=speaking_rate, style_prompt=style_prompt), section, os.getenv("DATABASE_URL", ""), exam_format)


def is_busy():
    return bool(runner.thread and runner.thread.is_alive())


with advanced:
    st.markdown("**모델 목록 관리**")
    render_bizrouter_sync(store, key="bulk-bizrouter-sync")
    if st.button("ChatKHU 모델 동기화", disabled=not has_chatkhu_api_key()):
        try:
            ids = list_chatkhu_models()
            store.set_setting("extra_models", json.dumps(list(dict.fromkeys([*json.loads(settings.get("extra_models", "[]")), *ids]))))
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    custom = st.text_input("텍스트 모델 ID 직접 추가", help="BizRouter 모델은 위 동기화 버튼으로 불러오세요.")
    if st.button("모델 추가", disabled=not custom.strip()):
        try:
            added = custom_model(custom)
            saved_id = added[0] if provider_backend(added[0]) == "bizrouter" else added[1]
            store.set_setting("extra_models", json.dumps(list(dict.fromkeys([*json.loads(settings.get("extra_models", "[]")), saved_id]))))
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

options = model_options(ROOT)
options.extend((BIZROUTER_PREFIX + item["id"], item["id"])
               for item in parse_bizrouter_models(settings.get("bizrouter_models", "[]")))
for model in json.loads(settings.get("extra_models", "[]")):
    try:
        options.append(custom_model(model))
    except ValueError:
        pass
options = list(dict.fromkeys(options))
options = [value for value in options if service == "all" or provider_backend(*value) == service]
capabilities = None
if not options:
    st.info("모델 목록 관리에서 이 서비스의 모델을 동기화하세요.")
    provider, model, preset = "", "", None
else:
    saved = tuple(json.loads(settings.get("last_model", '["gpt_5_6_luna","gpt-5.6-luna"]')))
    if st.session_state.get("bulk-model") not in options:
        st.session_state["bulk-model"] = saved if saved in options else options[0]
    selected = model_area.selectbox("모델", options, format_func=model_label, key="bulk-model")
    provider, model = selected
    store.set_setting("last_model", json.dumps(selected))
    presets = json.loads(settings.get("presets", "{}"))
    supported = supports_generation_presets(provider, model)
    preset_identity = provider if provider_backend(provider) == "bizrouter" else model
    saved_preset = presets.get(preset_identity, DEFAULT_GENERATION_PRESET)
    if saved_preset not in GENERATION_PRESETS:
        saved_preset = DEFAULT_GENERATION_PRESET
    preset_key = f"bulk-preset-{preset_identity}"
    if preset_key not in st.session_state:
        st.session_state[preset_key] = saved_preset
    preset_id = advanced.selectbox(
        "생성 프리셋", list(GENERATION_PRESETS),
        format_func=lambda value: GENERATION_PRESETS[value].label, disabled=not supported,
        key=preset_key,
    )
    preset = preset_id if supported else None
    if supported:
        presets[preset_identity] = preset_id
        store.set_setting("presets", json.dumps(presets))
        advanced.caption(GENERATION_PRESETS[preset_id].description)
    else:
        advanced.caption("프리셋 미지원 · 기본 API 설정")
    advanced.caption(f"실제 모델 ID: {model} · API {'연결됨' if can_gateway_call(provider) else '키 없음'}")
    capabilities = model_capabilities_for(ROOT, model) if provider_backend(provider) == "bizrouter" else None
    if advanced.checkbox("적용 API 설정 보기"):
        advanced.json(generation_parameter_preview(provider, model, preset, capabilities))


STATUS_LABELS = {"pending": "대기", "processing": "생성 중", "succeeded": "저장 완료",
                 "failed": "실패", "partial": "일부 완료", "interrupted": "중단 · 재개 가능", "running": "실행 중"}
PIPELINE_LABELS = {"pending": "대기", "generating": "문항 생성", "tts": "음성 합성", "compose": "시험 음원 조립",
    "image": "그림 생성", "chart": "도표 렌더링", "upload": "S3 업로드", "verify": "파일 검증", "register": "DB 등록",
    "succeeded": "DB 등록 완료", "failed": "미완료 · 재시도 가능", "paused": "중단 · 재개 가능"}
PIPELINE_LABELS.update(reviewing="이전 작업 · 재개 가능", ready="이전 작업 · 재개 가능", needs_attention="확인 필요 · 재개 가능")


polling = needs_progress_refresh(store.batches(), runner_active=is_busy())


@st.fragment(run_every="2s" if polling else None)
def render_progress():
    batches = store.batches()
    active = needs_progress_refresh(batches, runner_active=is_busy())
    signature = (active, tuple((batch["id"], batch["status"], batch["pipeline_status"]) for batch in batches))
    previous = st.session_state.get("bulk-status-signature")
    st.session_state["bulk-status-signature"] = signature
    if active != polling or (previous is not None and previous != signature):
        # Reconfigure the timer and refresh controls after a worker state change.
        st.rerun()
    if not batches:
        return
    st.subheader("생성 진행")
    refresh = st.button("등록 상태 새로 확인", key="bulk-refresh-registration")
    identity = (str(ROOT), os.getenv("DATABASE_URL", ""), signature)
    cached = st.session_state.get("bulk-registration-snapshot")
    if refresh or cached is None or cached[0] != identity or (active and time.monotonic() - cached[1] >= 30):
        cached = (identity, time.monotonic(), load_overview(ROOT, os.getenv("DATABASE_URL", "")))
        st.session_state["bulk-registration-snapshot"] = cached
    snapshot = cached[2]
    snapshots = {b["id"]: b for b in snapshot["batches"]}
    views = {b["id"]: set_view({**b, **snapshots.get(b["id"], {})}, sets=snapshot["bank"]["sets"],
                             bank_available=snapshot["bank"]["available"]) for b in batches}
    completed = completed_batches(snapshot)
    completed_ids = {b["id"] for b in completed}
    if not snapshot["bank"]["available"] or not snapshot["bank"]["media_available"] or snapshot["errors"]:
        st.warning("등록 완료 여부를 확인할 수 없어 작업 목록을 유지합니다. DB 연결·미디어 구조·로컬 기록을 확인하세요.")
    if completed:
        if st.session_state.get("bulk-completed-history") not in completed_ids:
            st.session_state.pop("bulk-completed-history", None)
        with st.expander(f"완료된 생성 이력 · {len(completed)}개", expanded=False):
            st.caption("전체 문항과 필수 미디어 연결이 확인된 세트입니다.")
            completed_id = st.selectbox("완료된 생성 묶음", [b["id"] for b in completed],
                format_func=lambda value: set_label(views[value]), key="bulk-completed-history")
            render_set_summary(views[completed_id])
            if st.button("이 세트를 서버에 등록", key="bulk-transfer"):
                st.session_state["bank-transfer-set-link"] = views[completed_id]["set_id"]
                st.switch_page(str(menu_page("bank_transfer_app.py")))
    batches = [b for b in batches if b["id"] not in completed_ids]
    if not batches:
        st.session_state.pop("bulk-history", None)
        st.info("진행 중이거나 미완료인 생성 작업이 없습니다.")
        return
    active_ids = [b["id"] for b in batches]
    if st.session_state.get("bulk-history") not in active_ids:
        st.session_state.pop("bulk-history", None)
    selected_id = st.selectbox(
        "생성 묶음", active_ids, key="bulk-history",
        format_func=lambda value: set_label(views[value]),
    )
    render_set_summary(views[selected_id])
    batch = next(value for value in batches if value["id"] == selected_id)
    batch_format = batch.get("exam_format", "pbt")
    total = question_count(batch_format)
    st.caption(f"시험 형식: {batch_format.upper()}")
    from topik_question_lab.common.local_cleanup import batch_was_cleaned
    if batch_was_cleaned(ROOT, selected_id):
        st.info("등록 후 로컬 정리한 묶음입니다. 현재 등록 상태는 문제 저장 현황에서 확인하세요.")
        if st.button("문제 저장 현황 열기"):
            st.switch_page(str(menu_page("storage_overview_app.py")))
        return
    tasks = store.tasks(selected_id)
    review_details = batch_review_details(ROOT, selected_id)
    review_counts = {task["type_id"]: review_details["counts"].get(task["type_id"], task["review_count"])
                     for task in tasks}
    count = sum(task["count"] for task in tasks)
    reviews = sum(review_counts.values())
    phase = PIPELINE_LABELS.get(batch["pipeline_status"], batch["pipeline_status"]) if batch["media_config"] != "{}" else STATUS_LABELS[batch["status"]]
    if batch["pipeline_status"] == "succeeded":
        phase = "로컬 작업 완료 · 현재 등록 상태 확인 필요"
    approved = review_details["approved"]
    st.progress(min(count / total, 1.0), text=f"{phase} · 문항 저장 {count}/{total} · 검수 완료 {approved}/{total} · 검사 지적 문항 {reviews}")
    with st.expander("작업 상세", expanded=False):
        st.caption(f"고정 모델: {batch['model']} · 고정 프리셋: {GENERATION_PRESETS[batch['preset']].label if batch['preset'] else '기본 API 설정'}")
        if batch["media_config"] != "{}":
            st.write(PIPELINE_LABELS.get(batch["pipeline_status"], batch["pipeline_status"]))
            media = store.media_tasks(selected_id)
            audio = [t for t in media if t["kind"] == "compose"]
            images = [t for t in media if t["kind"] in ("image", "chart")]
            done_audio = [t for t in audio if t["status"] == "succeeded"]
            connected = sum(len(json.loads(t["snapshot_json"])["script"]["positions"]) for t in done_audio)
            listening = batch["section"] == "listening"
            expected_images = sum(4 * len(p.question_numbers) for p in profiles_for("listening", batch_format).values() if p.visual_kind != "none") if listening else (4 if batch_format == "ibt" else 1)
            st.caption(f"생성 음원 {len(done_audio)}/{(22 if batch_format == 'ibt' else 35) if listening else 0}그룹 · 음원 대상 {connected}/{total if listening else 0}문항 · "
                       f"이미지 파일 {sum(t['status']=='succeeded' for t in images)}개 생성 · 필수 이미지 연결 {expected_images}개 · DB 연결은 전체 검증 후 반영")
            if batch["next_attempt_at"] > time.time():
                st.caption(f"다음 요청까지 {max(0, int(batch['next_attempt_at'] - time.time()))}초")
            if media:
                st.dataframe([{"단계": PIPELINE_LABELS.get(t["kind"], t["kind"]), "상태": t["status"], "시도": t["attempts"], "안내": t["error"]}
                              for t in media], hide_index=True, width="stretch")
        st.dataframe([{"번호": ", ".join(map(str, task["snapshot"]["slots"])), "유형": task["type_id"],
                       "상태": STATUS_LABELS[task["status"]], "저장": task["count"],
                       "검사 지적": review_counts[task["type_id"]], "시도": task["attempts"], "안내": task["error"]}
                      for task in tasks], hide_index=True, width="stretch")
    if review_details["errors"]:
        st.warning("일부 문항의 검사 결과를 읽지 못했습니다. 확인되지 않은 유형은 생성 당시 지적 건수를 표시합니다.")
        for error in review_details["errors"]:
            st.caption(error)
    if review_details["questions"]:
        st.subheader("검사 지적 상세")
        st.caption("오류가 있는 문항을 수정한 뒤 ‘이어서 완료’를 누르세요. 경고는 검토 권장 사항입니다.")
        for question in review_details["questions"]:
            section_label = "읽기" if question["section"] == "reading" else "듣기"
            for issue in question["issues"]:
                is_error = issue.get("severity", "error") == "error"
                label = "오류" if is_error else "경고"
                (st.error if is_error else st.warning)(f"{section_label} {question['slot']}번 · {label}: {issue['message']}")
            if st.button(f"{question['slot']}번 검수하기", key=f"bulk-review-{selected_id}-{question['type_id']}-{question['id']}"):
                st.session_state["registration-review-link"] = {key: question[key] for key in ("section", "type_id", "id")}
                st.session_state.pop("bulk-registration-snapshot", None)
                name = "app.py" if question["section"] == "reading" else "listening_app.py"
                st.switch_page(str(menu_page(name)))
    if batch["error"]:
        st.error(f"마지막 실행 중단 사유: {batch['error']}")
    if batch["pipeline_status"] == "succeeded":
        st.info("로컬 작업은 완료되었습니다. 현재 문항·미디어 등록 상태를 확인하지 못했거나 추가 확인이 필요해 진행 목록에 표시합니다.")
    elif batch["status"] == "succeeded" and batch["media_config"] == "{}":
        if approved == total:
            st.success(f"1~{total}번 생성과 검수 완료(자동 승인) 처리가 끝났습니다. ‘이어서 완료’를 누르면 미디어 준비와 세트 등록을 진행합니다.")
        else:
            st.info(f"1~{total}번 생성이 완료되었습니다. ‘이어서 완료’를 누르면 남은 작업을 진행합니다.")
    busy = is_busy() or any(b["status"] == "running" for b in store.batches())
    if st.button("이어서 완료", type="primary", disabled=busy, key="bulk-resume"):
        try:
            # Keep the original destination and media settings when resuming.
            config = json.loads(batch["media_config"]) or media_config(batch["section"], batch.get("exam_format", "pbt"))
            runner.start(batch_id=selected_id, media_config=config)
            st.session_state.pop("bulk-registration-snapshot", None)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    if busy and batch["auto_resume"] and st.button("현재 작업 중단", key="bulk-stop"):
        runner.stop(selected_id)
        st.rerun()


if model:
    label = "듣기" if section == "listening" else "읽기"
    files = list((ROOT / ("data/types" if section == "reading" else "data/listening/types")).glob("*.db"))
    topic_path = ROOT / "data/topic_bank.db"
    signature = (exam_format, MAPPING_PATH.stat().st_mtime_ns if exam_format == "ibt" else 0, provider, model, preset,
                 json.dumps(capabilities, sort_keys=True), tuple((str(path), path.stat().st_mtime_ns) for path in sorted(files)),
                 topic_path.stat().st_mtime_ns if topic_path.exists() else 0)
    with st.expander("생성 준비 상세", expanded=False):
        if st.button("준비 상태 새로 확인", key=f"bulk-refresh-{section}"):
            st.session_state.pop(f"bulk-plan-{section}", None)
        cached = st.session_state.get(f"bulk-plan-{section}")
        if cached is None or cached[0] != signature:
            snapshots, errors = prepare_batch(ROOT, section, provider, model, preset, exam_format)
            cached = (signature, snapshots, errors)
            st.session_state[f"bulk-plan-{section}"] = cached
        _, snapshots, errors = cached
        st.dataframe([{"번호": ", ".join(map(str, item["slots"])), "유형": item["type_id"],
                       "문항 수": item["count"], "승인 예시": item["approved_count"], "난이도": item["difficulty"]}
                      for item in snapshots], hide_index=True, width="stretch")
    for error in errors:
        st.error(error)
    busy = is_busy() or any(batch["status"] == "running" for batch in store.batches())
    action = "생성하고 등록" if auto_media else "문항만 생성"
    if not auto_media:
        st.info("문항만 생성하도록 설정되어 있습니다. 미디어와 세트 등록까지 진행하려면 고급 설정에서 자동 등록을 켜세요.")
    if st.button(f"{label} {expected_count}문제 {action}", type="primary", disabled=bool(errors) or busy,
                 key=f"bulk-start-{section}"):
        try:
            config = media_config(section, exam_format) if auto_media else None
            batch_id = runner.start(snapshots, media_config=config)
            st.session_state["bulk-history"] = batch_id
            st.session_state.pop("bulk-registration-snapshot", None)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

render_progress()
