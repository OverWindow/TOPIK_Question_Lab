from pathlib import Path

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


def test_all_stages_render_without_exception():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "app.py"), default_timeout=10).run()
    assert not app.exception
    assert any(button.label == "모델 선택" for button in app.sidebar.button)
    model_button = next(button for button in app.sidebar.button if button.label == "모델 선택")
    model_button.click().run()
    assert not app.exception
    picker = next(widget for widget in app.multiselect if widget.label == "사용할 모델")
    assert "GPT-5.6 Luna" in picker.options
    assert "Gemini 3.5 Flash" in picker.options
    assert any(button.label == "전체 선택" for button in app.button)
    assert any(button.label == "선택 모델 적용" for button in app.button)


def test_database_delete_dialog_requires_confirmation():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "app.py"), default_timeout=10).run()
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
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "app.py"), default_timeout=10).run()
    app.sidebar.selectbox[0].set_value("similar_expression").run()

    assert not app.exception
    assert any(field.label == "밑줄 대상 표현" for field in app.text_input)
    assert any(metric.label == "밑줄 지정 필요" for metric in app.metric)
    assert any(button.label == "저장하고 다음 문제" for button in app.button)


def test_representative_structured_type_editors_render():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "app.py"), default_timeout=15).run()

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
