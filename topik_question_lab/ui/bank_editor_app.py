"""Registered-question editor; opening and previewing never mutate the bank."""
from copy import deepcopy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.step_06_publication.bank_editor import (
    BankEditor, item_metadata, prepare_edit, question_profile, shared_members,
    audio_members, media_ready, set_health,
)
from topik_question_lab.step_06_publication.direct_publication import _hash
from topik_question_lab.step_06_publication.storage_overview import read_bank, REVIEW_LABELS, visual_requirements
from topik_question_lab.ui.registration_ui import show_question


SECTION = {"reading": "읽기", "listening": "듣기", "writing": "쓰기"}
ACTIONS = {"approve": "검수 승인", "retire": "사용 중지", "restore": "검수 대기로 복원", "delete": "영구 삭제"}


def refresh():
    for key in ("bank-editor-list", "bank-editor-snapshot", "bank-editor-plan", "registration-snapshot",
                "registration-plan", "storage-overview", "storage-file-check"):
        st.session_state.pop(key, None)


def media_url(value):
    try:
        url, base = urlsplit(value), urlsplit(os.getenv("CLOUDFRONT_BASE_URL", ""))
        if (url.scheme == "https" and url.netloc == base.netloc and url.hostname
                and not url.username and not url.password and not url.query and not url.fragment
                and url.path.startswith(base.path.rstrip("/") + "/")):
            return value
    except ValueError:
        pass
    return None


def show_media(snapshot):
    row, media = snapshot["item"], snapshot["media"]
    audio_ids = {b["audio_asset_id"] for b in media["item_audio_bindings"] if b["item_id"] == row["item_id"] and b["content_hash"] == row["content_hash"]}
    audio_ids.update(b["audio_asset_id"] for b in media["question_set_item_audio_bindings"] if b["set_id"] == row["set_id"] and b["position"] == row["position"])
    assets = [a for a in media["tts_audio_assets"] if a["audio_asset_id"] in audio_ids and a["deleted_at"] is None]
    assets += [a for a in media["item_visual_assets"] if a["item_id"] == row["item_id"] and a["content_hash"] == row["content_hash"]]
    st.caption("필수 미디어 연결 완료" if media_ready(snapshot, row) else "필수 음원·이미지 교체 필요")
    with st.expander("현재 음원·이미지"):
        if not assets:
            st.info("현재 연결된 파일이 없습니다.")
        for asset in assets:
            url = media_url(asset["storage_url"])
            if not url:
                st.caption("파일이 연결되어 있습니다. 미리보기는 설정된 CloudFront 주소를 지원합니다.")
            elif "audio_asset_id" in asset:
                st.audio(url)
            else:
                st.image(url, caption="자료 이미지" if asset["visual_role"] == "material" else f"선지 {asset['option_number']}")


def parse_dialogue(value):
    turns = []
    for line in value.splitlines():
        if not line.strip():
            continue
        speaker, separator, text = line.replace("：", ":", 1).partition(":")
        if not separator or not speaker.strip() or not text.strip():
            raise ValueError("대본은 한 줄에 ‘화자: 발화’ 형식으로 입력하세요.")
        turns.append({"speaker": speaker.strip(), "text": text.strip()})
    return turns


def edit_form(snapshot):
    row = snapshot["item"]
    question = deepcopy(row["content_json"])
    profile = question_profile(row)
    prefix = f"bank-edit-{row['item_id']}-{_hash(snapshot)[:12]}"
    members = shared_members(snapshot)
    if len(members) > 1:
        numbers = ", ".join(str(r["type_slot"]) for r in sorted(members, key=lambda r: r["type_slot"]))
        st.info(f"공통 지문·대본 수정은 {numbers}번에 함께 적용됩니다.")
    with st.form(prefix):
        if row["section"] == "reading":
            if profile.content_mode == "single_sentence":
                question["stem"] = st.text_area("문제 문장", question.get("stem", ""), height=120, key=f"{prefix}-stem")
            else:
                question["passage"] = st.text_area("지문·자료", question.get("passage", ""), height=240, key=f"{prefix}-passage")
            question["question_prompt"] = st.text_area("문항 질문", question.get("question_prompt", ""), height=80, key=f"{prefix}-question_prompt")
            question["auxiliary_text"] = st.text_area("보조 문장·자료", question.get("auxiliary_text", ""), height=80, key=f"{prefix}-auxiliary_text")
            question["highlight_text"] = st.text_input("밑줄 대상 표현", question.get("highlight_text", ""), key=f"{prefix}-highlight_text")
            question["target_grammar"] = st.text_input("목표 문법·기능", question.get("target_grammar", ""), key=f"{prefix}-target_grammar")
            st.text_area("도표·그림 설명 데이터 (JSON)", json.dumps(question.get("visual_material"), ensure_ascii=False, indent=2), height=130, key=f"{prefix}-material")
        else:
            st.text_area("듣기 대본 · 한 줄에 화자: 발화", "\n".join(f"{t['speaker']}: {t['text']}" for t in question.get("dialogue_turns", [])), height=260, key=f"{prefix}-dialogue")
            question["question_prompt"] = st.text_area("문항 질문", question.get("question_prompt", ""), height=80, key=f"{prefix}-question_prompt")
            question["target_skill"] = st.text_input("목표 듣기 기능", question.get("target_skill", ""), key=f"{prefix}-target_skill")
            question["repeat_count"] = st.selectbox("듣기 반복 횟수", [1, 2], index=int(question.get("repeat_count", 1)) - 1, key=f"{prefix}-repeat_count")
            question["question_role"] = st.text_input("문항 역할", question.get("question_role", ""), key=f"{prefix}-question_role")
            sources = ["official", "ai_suggested", "manual", "unknown"]
            question["answer_source"] = st.selectbox("정답 출처", sources, index=sources.index(question.get("answer_source", "unknown")), key=f"{prefix}-answer_source")
        choices = question.get("choices") or []
        if not question.get("visual_options") or choices:
            cols = st.columns(2)
            question["choices"] = [cols[n % 2].text_area(f"선지 {n + 1}", choices[n] if n < len(choices) else "", height=85, key=f"{prefix}-choice-{n}") for n in range(4)]
        for option in question.get("visual_options") or []:
            n = option["number"]
            st.markdown(f"**이미지 선지 {n}**")
            option["description"] = st.text_area(f"선지 {n} 그림 설명", option.get("description", ""), height=80, key=f"{prefix}-visual-{n}-description")
            option["image_prompt"] = st.text_area(f"선지 {n} 그림 생성 지시", option.get("image_prompt", ""), height=80, key=f"{prefix}-visual-{n}-image_prompt")
            st.text_area(f"선지 {n} 도표 데이터 (JSON)", json.dumps(option.get("chart_spec"), ensure_ascii=False, indent=2), height=100, key=f"{prefix}-visual-{n}-chart")
        question["answer"] = st.selectbox("정답", [1, 2, 3, 4], index=int(question.get("answer", 1)) - 1, key=f"{prefix}-answer")
        question["explanation"] = st.text_area("해설", question.get("explanation", ""), height=140, key=f"{prefix}-explanation")
        question["difficulty"] = st.text_input("난이도 설명", question.get("difficulty", ""), key=f"{prefix}-difficulty")
        meta = item_metadata(row)
        cols = st.columns(3)
        meta["target_level"] = cols[0].number_input("TOPIK 급수", 1, 6, int(meta["target_level"]), key=f"{prefix}-target_level")
        meta["predicted_difficulty"] = cols[1].number_input("예상 난이도", -3.0, 3.0, float(meta["predicted_difficulty"]), step=0.1, key=f"{prefix}-predicted_difficulty")
        meta["primary_skill"] = cols[2].text_input("평가 요소", meta["primary_skill"], key=f"{prefix}-primary_skill")
        with st.expander("소재 정보"):
            for field, label in (("topic_id", "소재 ID"), ("topic_domain", "소재 분야"), ("topic_title", "소재 제목"), ("topic_angle", "소재 관점")):
                question[field] = st.text_input(label, question.get(field, ""), key=f"{prefix}-{field}")
        upload_fields = []
        with st.expander("음원·이미지 파일 교체"):
            st.caption("교체 파일은 저장을 실행할 때 S3에 업로드하고 검증합니다. 내용이 바뀐 기존 미디어는 교체가 필요할 수 있습니다.")
            if row["section"] == "listening":
                numbers = ", ".join(str(r["type_slot"]) for r in sorted(audio_members(snapshot), key=lambda r: r["type_slot"]))
                st.file_uploader(f"{numbers}번 음원 교체 · MP3", type=["mp3"], key=f"{prefix}-audio")
                upload_fields.append(({"kind": "audio"}, f"{prefix}-audio"))
            for role, n in sorted(visual_requirements(row["section"], row["type_slot"], question)):
                label = "자료 이미지" if role == "material" else f"선지 {n} 이미지"
                key = f"{prefix}-image-{role}-{n}"
                st.file_uploader(f"{label} 교체", type=["png", "webp", "jpg", "jpeg"], key=key)
                upload_fields.append(({"kind": "image", "visual_role": role, "option_number": n}, key))

        def prepare():
            try:
                for field in list(question):
                    key = f"{prefix}-{field}"
                    if key in st.session_state:
                        question[field] = st.session_state[key]
                for field in meta:
                    meta[field] = st.session_state[f"{prefix}-{field}"]
                if question.get("choices"):
                    question["choices"] = [st.session_state[f"{prefix}-choice-{n}"] for n in range(4)]
                if row["section"] == "reading":
                    question["visual_material"] = json.loads(st.session_state[f"{prefix}-material"])
                    if question["visual_material"] is not None and not isinstance(question["visual_material"], dict):
                        raise ValueError("도표·그림 데이터는 JSON 객체 또는 null이어야 합니다.")
                else:
                    question["dialogue_turns"] = parse_dialogue(st.session_state[f"{prefix}-dialogue"])
                for option in question.get("visual_options") or []:
                    n = option["number"]
                    for field in ("description", "image_prompt"):
                        option[field] = st.session_state[f"{prefix}-visual-{n}-{field}"]
                    option["chart_spec"] = json.loads(st.session_state[f"{prefix}-visual-{n}-chart"])
                    if option["chart_spec"] is not None:
                        from topik_question_lab.common.listening_models import ChartSpec
                        option["chart_spec"] = ChartSpec.model_validate(option["chart_spec"]).model_dump()
                uploads = [{**descriptor, "data": st.session_state[key].getvalue()}
                           for descriptor, key in upload_fields if st.session_state.get(key)]
                plan = prepare_edit(snapshot, question, meta, uploads)
                st.session_state["bank-editor-plan"] = {"action": "edit", "plan": plan}
            except json.JSONDecodeError:
                st.session_state["bank-editor-error"] = "도표·그림 JSON 형식을 확인하세요."
            except ValueError as exc:
                st.session_state["bank-editor-error"] = str(exc)
        st.form_submit_button("변경 내용 확인", type="primary", disabled=row["review_status"] == "retired", on_click=prepare)


def confirm_plan(editor, prepared):
    action = prepared["action"]
    plan = prepared.get("plan")
    snapshot = plan["snapshot"] if action == "edit" else prepared["snapshot"]
    row = snapshot["item"]
    st.subheader("변경 내용 확인" if action == "edit" else f"{ACTIONS[action]} 확인")
    confirmed = True
    if action == "edit":
        affected = [r for r in snapshot["items"] if r["item_id"] in plan["affected"]]
        st.write("영향받는 문항: " + ", ".join(f"{r['type_slot']}번" for r in sorted(affected, key=lambda r: r["type_slot"])))
        labels = {r["item_id"]: f"{r['type_slot']}번" for r in affected}
        chosen = st.selectbox("비교할 문항", list(labels), format_func=labels.get)
        old = next(r for r in affected if r["item_id"] == chosen)
        new = {**old, **plan["changes"].get(chosen, {})}
        left, right = st.columns(2)
        with left:
            st.markdown("**현재 PostgreSQL 내용**")
            show_question(old["content_json"])
            st.json(item_metadata(old))
        with right:
            st.markdown("**저장할 내용**")
            show_question(new["content_json"])
            st.json(item_metadata(new))
        with st.expander("전체 문항 데이터 비교"):
            left, right = st.columns(2)
            left.json(old["content_json"])
            right.json(new["content_json"])
        st.info("수정 문항과 관련 세트는 검수 대기 상태로 저장됩니다.")
        if plan["invalidate_audio"]:
            st.warning("대본·발문·반복 횟수가 바뀌어 공유 음원 연결을 갱신합니다. 새 음원이 없으면 교체 필요 상태로 저장됩니다.")
        for replacement in plan["replacements"]:
            label = "공유 음원" if replacement["kind"] == "audio" else f"{replacement['visual_role']} 이미지 {replacement['option_number']}"
            st.write(f"교체 파일: {label} · {len(replacement['data']):,}바이트")
    else:
        show_question(row["content_json"])
        if action == "delete":
            st.warning("문항과 해당 미디어 연결을 영구 삭제합니다. 세트 소속 문항이면 세트가 구성 미완성 상태가 됩니다. S3 파일과 로컬 생성 기록은 보관됩니다.")
            confirmed = st.checkbox("이 문항을 영구 삭제하며 복구할 수 없음을 확인했습니다")
        elif action == "retire":
            st.info("문항을 보관하며 사용을 중지합니다. 소속 세트는 사용 중지 문항이 포함된 상태로 표시됩니다.")
        elif action == "restore":
            st.info("문항을 검수 대기로 복원합니다.")
    left, right = st.columns(2)
    left.button("편집으로 돌아가기", on_click=lambda: st.session_state.pop("bank-editor-plan", None))
    def execute():
        try:
            with st.spinner("변경 내용을 처리하고 있습니다."):
                result = editor.save(plan) if action == "edit" else editor.change_status(snapshot, action)
            refresh()
            st.session_state["bank-editor-message"] = result["message"]
        except ValueError as exc:
            st.session_state["bank-editor-error"] = str(exc)
        except Exception:
            st.session_state["bank-editor-error"] = "결과를 확인하지 못했습니다. 새로고침하여 현재 내용을 확인하세요."
    right.button("PostgreSQL에 저장" if action == "edit" else f"{ACTIONS[action]} 실행", type="primary", disabled=not confirmed, on_click=execute)


def render_app(url):
    st.title("PostgreSQL 문제 관리")
    st.caption("등록된 문제를 직접 수정합니다. 세트·유형·문항 번호와 생성 출처는 고정됩니다.")
    message = st.session_state.pop("bank-editor-message", None)
    if message:
        st.success(message)
    error = st.session_state.pop("bank-editor-error", None)
    if error:
        st.error(error)
    st.button("문제 목록 새로고침", on_click=refresh)
    if not url:
        st.info("PostgreSQL 연결을 설정하면 등록된 문제를 수정할 수 있습니다.")
        return
    try:
        editor = BankEditor(url)
        saved = st.session_state.get("bank-editor-list")
        if not saved or saved["target"] != editor.target:
            refresh()
            saved = {"target": editor.target, "bank": read_bank(url)}
            st.session_state["bank-editor-list"] = saved
        bank = saved["bank"]
        if not bank["available"]:
            st.error(bank["message"])
            return
        prepared = st.session_state.get("bank-editor-plan")
        if prepared:
            confirm_plan(editor, prepared)
            return
        exam = st.selectbox("시험 형식", ["all", "pbt", "ibt"], format_func=lambda x: "전체" if x == "all" else x.upper(), key="bank-editor-exam")
        items = [r for r in bank["items"] if exam == "all" or r.get("exam_format", "pbt") == exam]
        sets = {str(s["set_id"]): s for s in bank["sets"] if exam == "all" or s.get("exam_format", "pbt") == exam}
        filters = st.columns(3)
        section = filters[0].selectbox("영역", ["all", "reading", "listening"], format_func=lambda v: SECTION.get(v, "전체"))
        types = sorted({r["item_type"] for r in items if section == "all" or r["section"] == section})
        selected_type = filters[1].selectbox("유형 필터", ["all", *types], format_func=lambda v: "전체" if v == "all" else v)
        set_options = ["all", "none", *sorted(sets)]
        selected_set = filters[2].selectbox("세트 필터", set_options, format_func=lambda v: "전체" if v == "all" else "미배정" if v == "none" else f"{SECTION.get(sets[v]['section'])} · {sets[v]['generator_version']} · 세트 {sets[v]['set_sequence']}")
        search = st.text_input("문제 내용 검색").strip().lower()
        show_retired = st.checkbox("사용 중지 문항 포함")
        focus = st.session_state.pop("bank-editor-open-item", None)
        rows = [r for r in items if (section == "all" or r["section"] == section)
                and (selected_type == "all" or r["item_type"] == selected_type)
                and (selected_set == "all" or (not r["set_id"] if selected_set == "none" else str(r["set_id"]) == selected_set))
                and (show_retired or r["review_status"] != "retired")
                and (not search or search in json.dumps(r["content_json"], ensure_ascii=False).lower())]
        if focus:
            focused = next((r for r in items if str(r["item_id"]) == focus), None)
            if focused and focused not in rows:
                rows.append(focused)
        st.caption(f"{len(rows)}문항")
        if not rows:
            st.info("조건에 맞는 문제가 없습니다.")
            return
        st.dataframe([{"시험": r.get("exam_format", "pbt").upper(), "영역": SECTION.get(r["section"]), "번호": r["type_slot"], "유형": r["item_type"],
                       "상태": REVIEW_LABELS.get(r["review_status"], r["review_status"]), "문제": r["stem"][:90],
                       "문항 ID": str(r["item_id"])} for r in rows], hide_index=True, width="stretch")
        options = [str(r["item_id"]) for r in rows]
        if focus in options:
            st.session_state["bank-editor-choice"] = focus
        elif st.session_state.get("bank-editor-choice") not in options:
            st.session_state["bank-editor-choice"] = options[0]
        labels = {str(r["item_id"]): f"{SECTION.get(r['section'])} {r['type_slot']}번 · {r['item_type']} · {r['stem'][:45]} · {r['item_id']}" for r in rows}
        item_id = st.selectbox("편집할 문제", options, format_func=labels.get, key="bank-editor-choice")
        cached = st.session_state.get("bank-editor-snapshot")
        if not cached or cached["target"] != editor.target or cached["item"]["item_id"] != item_id:
            cached = editor.load(item_id)
            st.session_state["bank-editor-snapshot"] = cached
        row = cached["item"]
        st.caption(f"문항 ID: {row['item_id']} · 유형: {row['item_type']} · 번호: {row['type_slot']} · 상태: {REVIEW_LABELS.get(row['review_status'])}")
        if cached["set"]:
            health = set_health(cached["items"])
            st.info(f"세트 {cached['set']['set_sequence']} · {health['count']}/{health['expected_count']} · {health['label']}")
            if cached["set"]["review_status"] == "retired":
                st.warning("사용 중지된 세트입니다.")
        show_media(cached)
        edit_form(cached)
        st.subheader("검수·사용 중지·삭제")
        options = ["restore", "delete"] if row["review_status"] == "retired" else ["approve", "retire", "delete"]
        action = st.selectbox("문항 작업", options, format_func=ACTIONS.get)
        def prepare_action():
            st.session_state["bank-editor-plan"] = {"action": action, "snapshot": cached}
        st.button("작업 내용 확인", on_click=prepare_action)
    except ValueError as exc:
        st.error(str(exc))
    except Exception:
        st.error("문항 관리 화면을 읽지 못했습니다. DB 연결·스키마를 확인하고 새로고침하세요.")


ROOT = Path(os.getenv("TOPIK_LAB_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="PostgreSQL 문제 관리", page_icon="✏️", layout="wide")
render_app(os.getenv("DATABASE_URL", "").strip())
