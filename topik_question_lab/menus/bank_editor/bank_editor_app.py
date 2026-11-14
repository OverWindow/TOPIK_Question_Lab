"""Registered-question editor; opening and previewing never mutate the bank."""

from topik_question_lab.common.paths import application_root
from copy import deepcopy
import json
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.common.question_bank.bank_editor import BankEditor, item_metadata, prepare_edit, question_profile, shared_members, audio_members, media_ready, set_health
from topik_question_lab.menus.bank_editor.bank_media_generation import generate_replacements
from topik_question_lab.menus.bank_editor.bank_set_editor_ui import render_set_browser
from topik_question_lab.common.question_bank.direct_publication import _hash, read_batches, PublicationBlocked
from topik_question_lab.common.question_bank.storage_overview import read_bank, bank_coverage, REVIEW_LABELS, visual_requirements
from topik_question_lab.common.ui.question_display import show_question
from topik_question_lab.common.ui.set_identity import set_view, set_label, render_set_summary


SECTION = {"reading": "읽기", "listening": "듣기", "writing": "쓰기"}
ACTIONS = {"approve": "검수 승인", "retire": "사용 중지", "restore": "검수 대기로 복원", "delete": "영구 삭제"}


def refresh():
    for key in ("bank-editor-list", "bank-editor-table", "bank-editor-snapshot", "bank-editor-plan", "bank-editor-draft", "registration-snapshot",
                "registration-plan", "storage-overview", "storage-file-check"):
        st.session_state.pop(key, None)


def change_management_mode():
    for key in ("bank-editor-plan", "bank-editor-snapshot", "bank-editor-draft", "bank-editor-choice",
                "bank-editor-table", "bank-set-delete-plan"):
        st.session_state.pop(key, None)
    st.session_state["bank-editor-selected"] = []


def choose_item(item_id):
    st.session_state["bank-editor-choice"] = item_id
    for key in ("bank-editor-snapshot", "bank-editor-draft"):
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


def show_image_preview(asset):
    material = asset["visual_role"] == "material"
    label = "자료 이미지" if material else f"{'①②③④'[asset['option_number'] - 1]} 선지 이미지"
    url = media_url(asset["storage_url"])
    if url:
        st.image(url, caption=label, width=640 if material else "stretch")
    else:
        st.caption(f"{label}: 파일이 연결되어 있습니다. 미리보기는 설정된 CloudFront 주소를 지원합니다.")


def show_media(snapshot):
    row, media = snapshot["item"], snapshot["media"]
    audio_ids = {b["audio_asset_id"] for b in media["item_audio_bindings"] if b["item_id"] == row["item_id"] and b["content_hash"] == row["content_hash"]}
    assets = [a for a in media["tts_audio_assets"] if a["audio_asset_id"] in audio_ids and a["deleted_at"] is None]
    images = sorted(
        [a for a in media["item_visual_assets"] if a["item_id"] == row["item_id"] and a["content_hash"] == row["content_hash"]],
        key=lambda a: a["option_number"],
    )
    st.caption("필수 미디어 연결 완료" if media_ready(snapshot, row) else "필수 음원·이미지 교체 필요")
    if images or visual_requirements(row["section"], row["type_slot"], row["content_json"]):
        st.subheader("이미지 미리보기")
        if not images:
            st.info("현재 문항 내용에 연결된 이미지가 없습니다. 아래에서 이미지 파일을 등록하거나 교체하세요.")
        for asset in images:
            if asset["visual_role"] == "material":
                show_image_preview(asset)
        choices = [a for a in images if a["visual_role"] == "choice"]
        for start in range(0, len(choices), 2):
            for column, asset in zip(st.columns(2), choices[start:start + 2]):
                with column:
                    show_image_preview(asset)
    if assets or row["section"] == "listening":
        with st.expander("현재 음원"):
            if not assets:
                st.info("현재 연결된 음원이 없습니다.")
            for asset in assets:
                url = media_url(asset["storage_url"])
                if url:
                    st.audio(url)
                else:
                    st.caption("음원이 연결되어 있습니다. 미리보기는 설정된 CloudFront 주소를 지원합니다.")


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
    draft = st.session_state.get("bank-editor-draft")
    if draft and draft["snapshot"] != snapshot:
        draft = None
    question = deepcopy(draft["question"] if draft else row["content_json"])
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
        meta = dict(draft["metadata"]) if draft else item_metadata(row)
        cols = st.columns(3)
        meta["target_level"] = cols[0].number_input("TOPIK 급수", 1, 6, int(meta["target_level"]), key=f"{prefix}-target_level")
        meta["predicted_difficulty"] = cols[1].number_input("예상 난이도", -3.0, 3.0, float(meta["predicted_difficulty"]), step=0.1, key=f"{prefix}-predicted_difficulty")
        meta["primary_skill"] = cols[2].text_input("평가 요소", meta["primary_skill"], key=f"{prefix}-primary_skill")
        with st.expander("소재 정보"):
            for field, label in (("topic_id", "소재 ID"), ("topic_domain", "소재 분야"), ("topic_title", "소재 제목"), ("topic_angle", "소재 관점")):
                question[field] = st.text_input(label, question.get(field, ""), key=f"{prefix}-{field}")
        upload_fields = []

        def media_input(descriptor, key, label, extensions):
            previous = next((r for r in draft["replacements"] if all(r.get(k) == v for k, v in descriptor.items())), None) if draft else None
            st.radio(f"{label} 교체 방식", ["직접 업로드", "자동 생성", "기존 유지"], horizontal=True,
                     index=1 if previous and previous.get("generation") else 0, key=f"{key}-mode")
            st.file_uploader(f"{label} 교체", type=extensions, key=key, help="직접 업로드를 선택했을 때 사용합니다.")
            if previous and not previous.get("generation"):
                st.caption("앞서 선택한 업로드 파일이 보관되어 있습니다. 새 파일을 선택하면 바뀝니다.")
            upload_fields.append((descriptor, key, previous))

        with st.expander("음원·이미지 파일 교체"):
            st.caption("자동 생성은 ‘변경 내용 확인’을 누를 때 현재 입력한 대본·그림 설명·도표 데이터로 실행됩니다. 다시 누르면 새로 생성합니다.")
            st.caption("생성 결과는 다음 화면에서 확인합니다. 업로드·자동 생성 파일 모두 ‘PostgreSQL에 저장’을 눌러야 S3 업로드와 교체가 진행됩니다.")
            if row["section"] == "listening":
                numbers = ", ".join(str(r["type_slot"]) for r in sorted(audio_members(snapshot), key=lambda r: r["type_slot"]))
                st.caption(f"음원 교체 적용 문항: {numbers}번")
                media_input({"kind": "audio"}, f"{prefix}-audio", f"{numbers}번 음원 · MP3", ["mp3"])
            for role, n in sorted(visual_requirements(row["section"], row["type_slot"], question)):
                label = "자료 이미지" if role == "material" else f"선지 {n} 이미지"
                key = f"{prefix}-image-{role}-{n}"
                media_input({"kind": "image", "visual_role": role, "option_number": n}, key, label, ["png", "webp", "jpg", "jpeg"])

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
                uploads = []
                for descriptor, key, previous in upload_fields:
                    mode = st.session_state[f"{key}-mode"]
                    if mode == "자동 생성":
                        uploads.append({**descriptor, "generate": True})
                    elif mode == "직접 업로드":
                        if st.session_state.get(key):
                            uploads.append({**descriptor, "data": st.session_state[key].getvalue()})
                        elif previous and not previous.get("generation"):
                            uploads.append(previous)
                plan = prepare_edit(snapshot, question, meta, uploads, allow_generation=True)
                if any(r.get("generate") for r in uploads):
                    with st.spinner("선택한 미디어를 자동 생성하고 있습니다."):
                        status = st.empty()
                        plan = generate_replacements(plan, progress=status.caption)
                st.session_state["bank-editor-draft"] = plan
                st.session_state["bank-editor-plan"] = {"action": "edit", "plan": plan}
            except json.JSONDecodeError:
                st.session_state["bank-editor-error"] = "도표·그림 JSON 형식을 확인하세요."
            except ValueError as exc:
                st.session_state["bank-editor-error"] = str(exc)
            except Exception:
                st.session_state["bank-editor-error"] = "교체 파일을 준비하지 못했습니다. 입력과 생성 설정을 확인하세요. 기존 미디어는 변경하지 않았습니다."
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
            label = "음원" if replacement["kind"] == "audio" else "자료 이미지" if replacement["visual_role"] == "material" else f"선지 {replacement['option_number']} 이미지"
            source = "자동 생성" if replacement.get("generation") else "직접 업로드"
            st.write(f"교체 파일: {label} · {source} · {len(replacement['data']):,}바이트")
            try:
                if replacement["kind"] == "audio":
                    st.audio(replacement["data"], format="audio/mpeg")
                else:
                    st.image(replacement["data"], caption=f"교체할 {label}", width=640)
            except Exception:
                st.error("교체 파일을 미리볼 수 없습니다. 편집으로 돌아가 파일을 다시 선택하거나 생성하세요.")
                confirmed = False
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
            if st.session_state.get("bank-editor-selection-target") != editor.target:
                st.session_state["bank-editor-selected"] = []
                choose_item(None)
            st.session_state["bank-editor-selection-target"] = editor.target
            refresh()
            try:
                batches = read_batches(ROOT)
            except PublicationBlocked:
                batches = []
            saved = {"target": editor.target, "bank": read_bank(url), "batches": batches}
            st.session_state["bank-editor-list"] = saved
        bank = saved["bank"]
        if not bank["available"]:
            st.error(bank["message"])
            return
        views = {}
        for s in bank["sets"]:
            members = [i for i in bank["items"] if str(i["set_id"]) == str(s["set_id"])]
            views[str(s["set_id"])] = set_view({**s, "item_count": len(members),
                "coverage": bank_coverage(members) if bank["media_available"] else None}, batches=saved.get("batches", []))
        focus = st.session_state.pop("bank-editor-open-item", None)
        if focus:
            st.session_state.pop("bank-editor-plan", None)
            st.session_state["bank-editor-mode"] = "문항별"
        mode = st.radio("관리 단위", ["문항별", "세트별"], horizontal=True,
                        key="bank-editor-mode", on_change=change_management_mode)
        prepared = st.session_state.get("bank-editor-plan")
        if prepared:
            # Rebuild checked rows after returning from the confirmation screen.
            st.session_state.pop("bank-editor-table", None)
            snapshot = prepared["plan"]["snapshot"] if prepared["action"] == "edit" else prepared["snapshot"]
            if str(snapshot["item"]["set_id"]) in views:
                render_set_summary(views[str(snapshot["item"]["set_id"])])
            confirm_plan(editor, prepared)
            return
        if focus:
            focused = next((r for r in bank["items"] if str(r["item_id"]) == focus), None)
            st.session_state["bank-editor-selected"] = [focus] if focused else []
            st.session_state.pop("bank-editor-table", None)
            choose_item(focus if focused else None)
            if focused:
                st.session_state.update({"bank-editor-exam": focused.get("exam_format", "pbt"),
                    "bank-editor-section": focused["section"], "bank-editor-type": "all",
                    "bank-editor-set": str(focused["set_id"]) if focused["set_id"] else "none",
                    "bank-editor-search": "", "bank-editor-retired": focused["review_status"] == "retired"})
            else:
                st.warning("열려는 문항을 찾을 수 없습니다. 목록을 새로고침하세요.")
        if mode == "세트별":
            rows = render_set_browser(bank, views, editor, ROOT, refresh)
            if rows is None:
                return
            st.subheader("세트 문항")
        else:
            exam = st.selectbox("시험 형식", ["all", "pbt", "ibt"], format_func=lambda x: "전체" if x == "all" else x.upper(), key="bank-editor-exam")
            items = [r for r in bank["items"] if exam == "all" or r.get("exam_format", "pbt") == exam]
            sets = {str(s["set_id"]): s for s in bank["sets"] if exam == "all" or s.get("exam_format", "pbt") == exam}
            filters = st.columns(3)
            section = filters[0].selectbox("영역", ["all", "reading", "listening"], format_func=lambda v: SECTION.get(v, "전체"), key="bank-editor-section")
            types = sorted({r["item_type"] for r in items if section == "all" or r["section"] == section})
            if st.session_state.get("bank-editor-type") not in ["all", *types]:
                st.session_state["bank-editor-type"] = "all"
            selected_type = filters[1].selectbox("유형 필터", ["all", *types], format_func=lambda v: "전체" if v == "all" else v, key="bank-editor-type")
            set_options = ["all", "none", *sorted(sets)]
            if st.session_state.get("bank-editor-set") not in set_options:
                st.session_state["bank-editor-set"] = "all"
            selected_set = filters[2].selectbox("세트 필터", set_options, format_func=lambda v: "전체" if v == "all" else "미배정" if v == "none" else set_label(views[v]), key="bank-editor-set")
            search = st.text_input("문제 내용 검색", key="bank-editor-search").strip().lower()
            show_retired = st.checkbox("사용 중지 문항 포함", key="bank-editor-retired")
            rows = [r for r in items if (section == "all" or r["section"] == section)
                    and (selected_type == "all" or r["item_type"] == selected_type)
                    and (selected_set == "all" or (not r["set_id"] if selected_set == "none" else str(r["set_id"]) == selected_set))
                    and (show_retired or r["review_status"] != "retired")
                    and (not search or search in json.dumps(r["content_json"], ensure_ascii=False).lower())]
            st.caption(f"{len(rows)}문항")
            if not rows:
                st.session_state["bank-editor-selected"] = []
                st.session_state.pop("bank-editor-table", None)
                choose_item(None)
                if selected_set in views:
                    render_set_summary(views[selected_set])
                st.info("조건에 맞는 문제가 없습니다.")
                return
        display_rows = [{"시험": r.get("exam_format", "pbt").upper(), "영역": SECTION.get(r["section"]), "번호": r["type_slot"], "유형": r["item_type"],
                       "세트": set_label(views[str(r["set_id"])]) if str(r["set_id"]) in views else "미배정",
                       "상태": REVIEW_LABELS.get(r["review_status"], r["review_status"]), "문제": r["stem"][:90],
                       "문항 ID": str(r["item_id"])} for r in rows]
        options = [str(r["item_id"]) for r in rows]
        identity = _hash((editor.target, display_rows))
        table = st.session_state.get("bank-editor-table")
        if not table or table["identity"] != identity:
            selected = set(st.session_state.get("bank-editor-selected", [])) & set(options)
            table = {"identity": identity, "rows": [{"선택": r["문항 ID"] in selected, **r} for r in display_rows]}
            st.session_state["bank-editor-table"] = table
            st.session_state["bank-editor-table-version"] = st.session_state.get("bank-editor-table-version", 0) + 1
        checked = st.data_editor(table["rows"], hide_index=True, width="stretch", num_rows="fixed",
            disabled=list(display_rows[0]),
            column_config={"선택": st.column_config.CheckboxColumn("선택", required=True), "문항 ID": None},
            key=f"bank-editor-table-{st.session_state['bank-editor-table-version']}")
        selected = [r["문항 ID"] for r in checked if r["선택"] and r["문항 ID"] in options]
        st.session_state["bank-editor-selected"] = selected
        if not selected:
            choose_item(None)
            st.info("표에서 수정할 문제를 체크하세요.")
            return
        if st.session_state.get("bank-editor-choice") not in selected:
            choose_item(selected[0])
        item_id = st.session_state["bank-editor-choice"]
        index = selected.index(item_id)
        st.caption(f"선택 {len(selected)}문항 · 현재 {index + 1}/{len(selected)}")
        if len(selected) > 1:
            left, right = st.columns(2)
            left.button("이전 선택 문제", disabled=index == 0, key="bank-editor-previous",
                        on_click=choose_item, args=(selected[max(0, index - 1)],))
            right.button("다음 선택 문제", disabled=index == len(selected) - 1, key="bank-editor-next",
                         on_click=choose_item, args=(selected[min(len(selected) - 1, index + 1)],))
            st.caption("이동하기 전에 수정 내용을 확인하고 저장하세요.")
        cached = st.session_state.get("bank-editor-snapshot")
        if not cached or cached["target"] != editor.target or cached["item"]["item_id"] != item_id:
            cached = editor.load(item_id)
            st.session_state["bank-editor-snapshot"] = cached
        row = cached["item"]
        if str(row["set_id"]) in views:
            render_set_summary(views[str(row["set_id"])])
        else:
            st.info("선택한 문항: 세트 미배정")
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


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="PostgreSQL 문제 관리", page_icon="✏️", layout="wide")
render_app(os.getenv("DATABASE_URL", "").strip())
