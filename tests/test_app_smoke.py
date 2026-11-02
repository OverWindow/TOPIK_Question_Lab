from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
STAGES = [
    "1. 기출 데이터",
    "2. 유형 분석",
    "3. 프롬프트 작업실",
    "4. 문제 생성",
    "5. 검수·비교",
    "6. 내보내기",
]


@pytest.fixture(autouse=True)
def synthetic_reading_examples(tmp_path):
    from topik_question_lab.common.models import QuestionExample
    from topik_question_lab.step_02_planning.prompt_profiles import QUESTION_TYPE_PROFILES
    from topik_question_lab.common.storage import Storage

    for type_id, profile in QUESTION_TYPE_PROFILES.items():
        db = tmp_path / "data/types" / f"{type_id}.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        storage = Storage(db)
        storage.upsert_examples([
            QuestionExample(source_exam="synthetic", question_number=number,
                instruction="알맞은 답을 고르십시오.", stem="밑줄표현 합성 예시 ( )",
                choices=["가", "나", "다", "라"], question_type=type_id,
                passage="합성 지문 ( )", question_prompt="알맞은 것은?",
                auxiliary_text="주어진 문장", set_key=f"synthetic:{type_id}")
            for number in profile.question_numbers
        ])


def test_all_stages_render_without_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(tmp_path))
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "ui" / "app.py"), default_timeout=10).run()
    assert not app.exception
    assert any(button.label == "모델 선택" for button in app.sidebar.button)
    model_button = next(button for button in app.sidebar.button if button.label == "모델 선택")
    model_button.click().run()
    assert not app.exception
    picker = next(widget for widget in app.multiselect if widget.label == "사용할 모델")
    assert "GPT-5.6 Luna" in picker.options
    assert "GPT-6 Luna" in picker.options
    assert "Gemini 3.5 Flash" in picker.options
    assert "DeepSeek V4 Pro" in picker.options
    assert any(button.label == "전체 선택" for button in app.button)
    assert any(button.label == "선택 모델 적용" for button in app.button)


def test_postgres_publication_page_renders_without_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    app = AppTest.from_file(
        str(ROOT / "topik_question_lab" / "ui" / "postgres_publish_app.py"), default_timeout=20
    ).run()

    assert not app.exception
    assert any(title.value == "문제 등록·세트 만들기" for title in app.title)
    assert any(button.label == "문제 추가하기" for button in app.button)
    assert any(button.label == "50문제 세트 만들기" for button in app.button)
    assert any("승인·자동검사 통과 문항이 없습니다" in item.value for item in app.info)
    assert next(m for m in app.metric if m.label == "등록된 세트").value == "확인 불가"


def test_database_delete_dialog_requires_confirmation():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "ui" / "app.py"), default_timeout=10).run()
    database_button = next(button for button in app.sidebar.button if button.label == "유형 DB 관리")
    database_button.click().run()

    assert not app.exception
    delete_button = next(button for button in app.button if button.label == "유형 DB 영구 삭제")
    assert delete_button.disabled
    assert any(field.label == "확인을 위해 유형 ID 입력" for field in app.text_input)

    for stage in STAGES[1:]:
        app.sidebar.radio[0].set_value(stage).run()
        assert not app.exception, stage


def test_similar_expression_database_and_highlight_editor_render():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "ui" / "app.py"), default_timeout=10).run()
    app.sidebar.selectbox[0].set_value("similar_expression").run()

    assert not app.exception
    assert any(field.label == "밑줄 대상 표현" for field in app.text_input)
    assert any(metric.label == "밑줄 지정 필요" for metric in app.metric)
    assert any(button.label == "저장하고 다음 문제" for button in app.button)


def test_enrichment_targets_can_be_selected_for_paired_46_47():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "ui" / "app.py"), default_timeout=15).run()
    app.sidebar.selectbox[0].set_value("paired_46_47").run()

    assert not app.exception
    target_picker = next(widget for widget in app.multiselect if widget.label == "AI 보완 대상 문제")
    assert len(target_picker.options) > 1
    assert len(target_picker.value) == len(target_picker.options)

    selected_source_key = target_picker.value[0]
    target_picker.set_value([selected_source_key]).run()
    assert not app.exception
    target_picker = next(widget for widget in app.multiselect if widget.label == "AI 보완 대상 문제")
    assert target_picker.value == [selected_source_key]
    assert any("선택 1개 / 보완 필요" in caption.value for caption in app.caption)


def test_representative_structured_type_editors_render():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "ui" / "app.py"), default_timeout=15).run()

    app.sidebar.selectbox[0].set_value("short_text_topic").run()
    assert not app.exception
    assert any(field.label == "짧은 글·안내문·도표" for field in app.text_area)
    assert any(metric.label == "구조 확인 필요" for metric in app.metric)

    app.sidebar.selectbox[0].set_value("sentence_insertion").run()
    assert not app.exception
    assert any(field.label == "주어진 문장" for field in app.text_area)

    app.sidebar.selectbox[0].set_value("paired_48_50").run()
    assert not app.exception
    assert any("paired_48_50.db" in caption.value for caption in app.sidebar.caption)

    app.sidebar.selectbox[0].set_value("paired_23_24").run()
    assert not app.exception
    assert any(field.label == "밑줄 대상 표현" for field in app.text_input)
    assert any("[[밑줄 부분]] 표시 가능" in field.label for field in app.text_area)
    assert any(metric.label == "밑줄 지정 필요" for metric in app.metric)

    app.sidebar.selectbox[0].set_value("paired_42_43").run()
    assert not app.exception
    assert any(field.label == "밑줄 대상 표현" for field in app.text_input)
    assert any("[[밑줄 부분]] 표시 가능" in field.label for field in app.text_area)
    assert any(metric.label == "밑줄 지정 필요" for metric in app.metric)
