import json
import os

import streamlit as st
from dotenv import load_dotenv

from topik_question_lab.common.generation.bulk_generation import BulkStore
from topik_question_lab.common.media.media_provider import (
    DEFAULT_BIZROUTER_IMAGE_MODEL, GOOGLE_IMAGE_MODELS, IMAGE_PROVIDERS,
    DEFAULT_BIZROUTER_TTS_MODEL, GOOGLE_TTS_MODELS, TTS_PROVIDERS, GEMINI_VOICES, OPENAI_VOICES,
    BizRouterImageClient, BizRouterSpeechClient, GoogleMediaClient, MediaError, MediaSettings,
    bizrouter_tts_family, saved_media_settings,
)
from topik_question_lab.common.paths import application_root


ROOT = application_root()
load_dotenv(ROOT / ".env")

st.set_page_config(page_title="설정", page_icon="⚙️", layout="wide")
st.title("설정")
audio_summary = st.empty()
image_summary = st.empty()
st.subheader("이미지 생성")
st.caption("새 일괄 생성과 문제 관리의 이미지 재생성에 적용됩니다.")
current = MediaSettings.from_env()
saved = saved_media_settings()

for service, label, client_type, configured in (
    ("bizrouter", "BizRouter", BizRouterImageClient, bool(os.getenv("BIZROUTER_API_KEY"))),
    ("google", "Google", GoogleMediaClient, bool(current.project)),
):
    if st.button(f"{label} 이미지 모델 동기화", disabled=not configured):
        client = None
        try:
            client = client_type(MediaSettings.from_env(image_provider=service))
            models = client.list_models()
            if models:
                key = f"{service}_image_models"
                saved[key] = json.dumps(models)
                BulkStore(ROOT).set_setting(key, saved[key])
                st.success(f"{label} 이미지 모델 {len(models)}개를 불러왔습니다.")
            else:
                st.warning(f"동기화할 {label} 이미지 모델이 없습니다. 기존 목록과 선택을 유지합니다.")
        except MediaError as exc:
            st.error(str(exc))
        finally:
            if client is not None:
                client.close()

catalogs = {}
for service in IMAGE_PROVIDERS:
    try:
        catalog = json.loads(saved.get(f"{service}_image_models", "[]"))
    except (ValueError, TypeError):
        catalog = []
    catalogs[service] = [model for model in catalog if isinstance(model, str) and model.strip()] if isinstance(catalog, list) else []
bizrouter_model = MediaSettings.from_env(image_provider="bizrouter").image_model
google_model = MediaSettings.from_env(image_provider="google").image_model
bizrouter_options = list(dict.fromkeys([bizrouter_model, DEFAULT_BIZROUTER_IMAGE_MODEL, *catalogs["bizrouter"]]))
google_options = list(dict.fromkeys([google_model, *(catalogs["google"] or GOOGLE_IMAGE_MODELS)]))
st.caption("BizRouter 동기화에는 BIZROUTER_API_KEY가 필요합니다. Google 동기화에는 GOOGLE_CLOUD_PROJECT_ID와 기존 Google 인증이 필요합니다.")
st.caption("Google은 이미지용 Gemini 모델을 불러옵니다. 4:1 배너를 지원하지 않는 구형 Gemini 2.x는 동기화 목록에서 제외합니다.")

with st.form("image-provider-settings"):
    provider = st.radio("이미지 생성 서비스", list(IMAGE_PROVIDERS),
                        index=list(IMAGE_PROVIDERS).index(current.image_provider),
                        format_func=IMAGE_PROVIDERS.get, key="image-provider")
    bizrouter_model = st.selectbox("BizRouter 이미지 모델", bizrouter_options, key="image-model-bizrouter")
    google_model = st.selectbox("Google 이미지 모델", google_options, key="image-model-google",
                                format_func=lambda model: f"{GOOGLE_IMAGE_MODELS[model]} · {model}" if model in GOOGLE_IMAGE_MODELS else model)
    if st.form_submit_button("저장"):
        # Keep the active service and both model choices consistent for new jobs.
        with BulkStore(ROOT).connect() as connection:
            connection.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", [
                ("image_provider", provider), ("image_model_bizrouter", bizrouter_model), ("image_model_google", google_model),
            ])
        current = MediaSettings.from_env()
        st.success(f"이미지 생성 서비스와 서비스별 모델을 저장했습니다. 현재 서비스: {IMAGE_PROVIDERS[provider]}")

st.write(f"현재 설정: **{IMAGE_PROVIDERS[current.image_provider]}**")
st.caption(f"이미지 모델: {current.image_model}")
st.caption("저장한 선택은 앱을 다시 열어도 유지됩니다. 기존 작업을 이어서 실행하면 해당 작업에 저장된 서비스를 사용합니다.")

st.subheader("음성 생성")
st.caption("새 듣기 일괄 생성, 문제 관리의 음원 재생성, 단어장 음성에 적용됩니다.")
if st.button("BizRouter 음성 모델 동기화", disabled=not bool(os.getenv("BIZROUTER_API_KEY"))):
    client = None
    try:
        client = BizRouterSpeechClient(MediaSettings.from_env(tts_provider="bizrouter"))
        models = client.list_models()
        if models:
            saved["bizrouter_tts_models"] = json.dumps(models)
            BulkStore(ROOT).set_setting("bizrouter_tts_models", saved["bizrouter_tts_models"])
            st.success(f"BizRouter 음성 모델 {len(models)}개를 불러왔습니다.")
        else:
            st.warning("동기화할 BizRouter 음성 모델이 없습니다. 기존 목록과 선택을 유지합니다.")
    except MediaError as exc:
        st.error(str(exc))
    finally:
        if client is not None:
            client.close()

try:
    tts_catalog = json.loads(saved.get("bizrouter_tts_models", "[]"))
except (ValueError, TypeError):
    tts_catalog = []
tts_catalog = [m for m in tts_catalog if isinstance(m, str) and bizrouter_tts_family(m)] if isinstance(tts_catalog, list) else []
tts_provider = st.radio("음성 생성 서비스", list(TTS_PROVIDERS),
                        index=list(TTS_PROVIDERS).index(current.tts_provider),
                        format_func=TTS_PROVIDERS.get, key="tts-provider")
tts_values = {"tts_provider": tts_provider}
for service, label in TTS_PROVIDERS.items():
    config = MediaSettings.from_env(tts_provider=service)
    defaults = GOOGLE_TTS_MODELS if service == "google" else [DEFAULT_BIZROUTER_TTS_MODEL, *tts_catalog]
    options = list(dict.fromkeys([config.tts_model, *defaults]))
    model = st.selectbox(f"{label} 음성 모델", options, key=f"tts-model-{service}")
    family = bizrouter_tts_family(model) if service == "bizrouter" else "gemini"
    voices = OPENAI_VOICES if family == "openai" else GEMINI_VOICES
    tts_values[f"tts_model_{service}"] = model
    for role, title, default in (("female", "여자 화자", "coral" if family == "openai" else "Aoede"),
                                 ("male", "남자 화자", "echo" if family == "openai" else "Charon")):
        selected = getattr(config, role)
        choices = list(voices)
        if service == "google" and selected not in choices:
            choices.insert(0, selected)
        tts_values[f"tts_{role}_{service}"] = st.selectbox(
            f"{label} {title} 목소리", choices, index=choices.index(selected if selected in choices else default),
            key=f"tts-{role}-{service}-{family}")
    if service == "bizrouter" and model.startswith("google/gemini-3.8-"):
        st.caption("Gemini 3.8은 스타일 지시 없이 대본만 낭독합니다.")

st.caption("목소리는 대본의 남녀 화자에 연결됩니다. BizRouter Gemini의 낭독 속도는 생성 후 조절합니다.")
if st.button("음성 설정 저장"):
    with BulkStore(ROOT).connect() as connection:
        connection.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", tts_values.items())
    current = MediaSettings.from_env()
    st.success(f"음성 생성 서비스와 서비스별 모델·목소리를 저장했습니다. 현재 서비스: {TTS_PROVIDERS[current.tts_provider]}")
audio_summary.info(f"현재 음성 서비스: **{TTS_PROVIDERS[current.tts_provider]}** · 모델: `{current.tts_model}`")
image_summary.info(f"현재 이미지 서비스: **{IMAGE_PROVIDERS[current.image_provider]}** · 모델: `{current.image_model}`")
st.caption(f"현재 음성 목소리: 여자 {current.female} / 남자 {current.male}")
st.caption("기본 서비스는 Google입니다. 기존 일괄 작업을 재개하면 해당 작업에 저장된 음성 설정을 사용합니다.")
