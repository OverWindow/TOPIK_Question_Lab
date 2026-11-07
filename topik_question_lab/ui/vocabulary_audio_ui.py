"""Audio controls embedded in the existing vocabulary page."""
import streamlit as st

from topik_question_lab.step_03_generation.media_provider import MediaSettings
from topik_question_lab.step_06_publication.media_cloud import cloudfront_url
from topik_question_lab.step_06_publication.vocabulary import search_vocabulary
from topik_question_lab.step_06_publication.vocabulary_audio import (
    MAX_BATCH_SIZE, VocabularyAudioError, audio_profile, audio_status,
    generate_vocabulary_audio, prepare_audio_schema,
)


STATUS_LABELS = {"missing": "없음", "ready": "있음", "stale": "갱신 필요"}


def current_audio_settings():
    saved = st.session_state.get("vocabulary-audio-preferences", {"speaker": "여자", "rate": 1.0})
    preferences = {"speaker": st.session_state.get("vocabulary-audio-speaker", saved["speaker"]),
                   "rate": st.session_state.get("vocabulary-audio-rate", saved["rate"])}
    st.session_state["vocabulary-audio-preferences"] = preferences
    return MediaSettings.from_env(speaking_rate=preferences["rate"])


def render_audio_controls(database_url, root, snapshot, selected, *, level, query, settings, speaker):
    st.markdown("**단어 음성**")
    if not snapshot["audio_schema"]:
        st.info("음성 기능 준비를 누르면 기존 단어장을 백업하고 음성 저장 컬럼을 추가합니다.")
        if st.button("음성 기능 준비", key="vocabulary-audio-prepare"):
            try:
                with st.spinner("단어장 백업·음성 기능 준비 중"):
                    backup = prepare_audio_schema(database_url, root)
                st.session_state["vocabulary-audio-notice"] = (
                    f"음성 기능을 준비했습니다. 백업: {backup}" if backup else "음성 기능이 이미 준비되어 있습니다.")
                st.rerun()
            except VocabularyAudioError as exc:
                st.error(str(exc))
        return

    if notice := st.session_state.pop("vocabulary-audio-notice", None):
        st.success(notice)
    profile = audio_profile(settings, speaker)
    state = audio_status(selected, profile)
    if selected.get("audio_storage_path"):
        try:
            if selected["audio_storage_bucket"] != settings.bucket:
                raise ValueError()
            st.audio(cloudfront_url(settings.base_url, selected["audio_storage_path"]), format="audio/mpeg")
            st.caption(f"재생 길이 {selected['audio_duration_ms'] / 1000:.1f}초")
        except ValueError:
            st.info("이 음성이 저장된 S3 버킷과 CloudFront 설정을 확인하세요.")
    else:
        st.caption("아직 생성된 음성이 없습니다.")
    if state == "stale":
        st.info("현재 단어 또는 생성 설정이 저장된 음성과 다릅니다. 다시 생성할 수 있습니다.")

    with st.expander("음성 생성 설정", expanded=False):
        st.selectbox("화자", ["여자", "남자"], index=["여자", "남자"].index(speaker), key="vocabulary-audio-speaker")
        st.number_input("낭독 속도", min_value=0.8, max_value=1.2, value=settings.speaking_rate, step=0.05,
                        key="vocabulary-audio-rate")
        st.caption(f"모델: {settings.tts_model} · 목소리: {profile['voice']} · 한국어 단어를 한 번 읽습니다.")
        st.caption("생성은 Google TTS를 사용하며 MP3를 S3에 저장합니다. 실행 중에는 화면을 유지하세요.")

    configured = bool(settings.project and settings.bucket and settings.base_url)
    if not configured:
        st.info("Google TTS 프로젝트, S3 버킷, CloudFront 주소를 설정하면 음성을 생성할 수 있습니다.")
    single = st.button("선택 단어 음성 재생성" if selected.get("audio_storage_path") else "선택 단어 음성 생성",
                       key="vocabulary-audio-single", disabled=not configured)

    with st.expander("음성 일괄 생성", expanded=False):
        scope = st.selectbox("생성 대상", ["missing", "stale"],
                             format_func=lambda value: "음성 없는 단어" if value == "missing" else "갱신 필요한 단어",
                             key="vocabulary-audio-scope")
        limit = st.number_input("한 번에 생성할 단어 수", min_value=1, max_value=MAX_BATCH_SIZE,
                                value=20, step=1, key="vocabulary-audio-limit")
        batch = search_vocabulary(database_url, level=level, query=query, audio_filter=scope,
                                  audio_profile=profile, page_size=limit)
        batch_ids = [row["vocabulary_id"] for row in batch["rows"]]
        st.caption(f"현재 급수·검색어 범위에서 대상 {batch['total']:,}개 중 앞의 {len(batch_ids)}개를 생성합니다.")
        if not batch["available"]:
            st.warning(batch["message"])
        bulk = st.button("대상 단어 음성 생성", key="vocabulary-audio-batch",
                         disabled=not configured or not batch_ids or not batch["available"])
        st.caption("완료된 단어는 다음 실행에서 제외됩니다. 실패한 단어는 다음 실행으로 재시도할 수 있습니다.")

    if single or bulk:
        bar = st.progress(0, text="음성 생성을 준비합니다.")

        def progress(done, total, word, message):
            bar.progress(done / total, text=f"{done}/{total} · {word} · {message}")

        try:
            results = generate_vocabulary_audio(database_url, root, [selected["vocabulary_id"]] if single else batch_ids,
                                                settings=settings, speaker=speaker, regenerate=single,
                                                progress=progress)
            st.session_state["vocabulary-audio-results"] = results
            st.rerun()
        except VocabularyAudioError as exc:
            st.error(str(exc))
    render_audio_results()


def render_audio_results():
    results = st.session_state.get("vocabulary-audio-results", [])
    if not results:
        return
    counts = {state: sum(row["status"] == state for row in results) for state in ("generated", "skipped", "failed")}
    st.caption(f"최근 실행: 생성 {counts['generated']} · 기존 음성 유지 {counts['skipped']} · 실패 {counts['failed']}")
    if counts["failed"]:
        st.warning("일부 단어의 생성·저장 결과를 확인하지 못했습니다. 아래 결과를 확인하고 다시 실행하세요.")
    else:
        st.success("선택한 단어의 음성 처리를 완료했습니다.")
    st.dataframe([{"단어": row["word"], "결과": row["message"]} for row in results],
                 hide_index=True, width="stretch")
