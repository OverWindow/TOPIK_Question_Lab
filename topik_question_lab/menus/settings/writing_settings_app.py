"""Choose the default service and model for writing analysis and generation."""
from dotenv import load_dotenv
import streamlit as st

from topik_question_lab.common.generation.providers import SERVICE_LABELS, provider_backend
from topik_question_lab.common.paths import application_root
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing.writing_prompt_ui import render_provider
from topik_question_lab.menus.writing.writing_settings import generation_default, save_generation_default


ROOT = application_root()
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="쓰기 설정", page_icon="✍️", layout="wide")
st.title("쓰기 설정")
st.subheader("기본 모델")
st.caption("쓰기 51~54번 유형 분석과 문제 생성에 공통으로 적용합니다. 각 화면에서 이번 실행의 모델을 바꿀 수도 있습니다.")

storage = WritingStorage(ROOT / "data/writing_pbt.db")
provider, model, _ = render_provider(storage, prefix="writing-settings", operation="기본", use_saved_default=True)
if st.button("쓰기 기본 모델 저장", type="primary", key="writing-settings-save", disabled=not model):
    save_generation_default(storage, provider, model)
    st.session_state["writing-settings-saved"] = True
    st.rerun()
if st.session_state.pop("writing-settings-saved", False):
    st.success("쓰기 유형 분석·문제 생성의 기본 모델을 저장했습니다.")

current = generation_default(storage)
st.info(f"현재 기본 서비스: **{SERVICE_LABELS[provider_backend(current['provider'])]}** · 모델: `{current['model']}`")
st.caption("저장한 기본값은 쓰기 유형 분석·문제 생성 화면을 열 때 적용되며 앱을 다시 열어도 유지됩니다.")
