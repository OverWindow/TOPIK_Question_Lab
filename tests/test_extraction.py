from pathlib import Path

from scripts.extract_topik_html_questions import html_blocks, split_questions


ROOT = Path(__file__).resolve().parents[1]


def test_recovers_one_missing_number_from_section_range_and_next_number():
    questions = split_questions(
        [
            "※ [1~2] 다음을 읽고 고르십시오.",
            "1번 지문이지만 번호가 누락되었다.",
            "① 보기1\n② 보기2\n③ 보기3\n④ 보기4",
            "2.",
            "2번 지문",
            "① 보기1\n② 보기2\n③ 보기3\n④ 보기4",
        ]
    )

    assert [question["number"] for question in questions] == [1, 2]
    assert "1번 지문이지만 번호가 누락되었다." in questions[0]["lines"]


def test_64th_html_recovers_all_questions_after_missing_28():
    path = ROOT / "html변환" / "64th-TOPIK-II-Reading-Test-Paper_Parse.html"
    questions = split_questions(html_blocks(path))

    assert [question["number"] for question in questions] == list(range(1, 51))
    question_28 = next(question for question in questions if question["number"] == 28)
    assert any("새해에 세운 목표" in line for line in question_28["lines"])
    assert any(line.startswith("④ 실천 가능한 계획") for line in question_28["lines"])


def test_60th_html_recovers_all_questions_after_missing_38():
    path = ROOT / "html변환" / "60th-TOPIK-II-Reading-Test-Paper_Parse.html"
    questions = split_questions(html_blocks(path))

    assert [question["number"] for question in questions] == list(range(1, 51))
    question_38 = next(question for question in questions if question["number"] == 38)
    assert any("분자 요리" in line for line in question_38["lines"])
