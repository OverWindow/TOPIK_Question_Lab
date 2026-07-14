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

    for stage in STAGES[1:]:
        app.sidebar.radio[0].set_value(stage).run()
        assert not app.exception, stage
