from pathlib import Path

from topik_question_lab.parser import scan_extracted_text


ROOT = Path(__file__).resolve().parents[1]


def test_scans_all_questions_and_sorts_choices():
    examples = scan_extracted_text(ROOT / "extracted_text")

    assert len(examples) == 16
    assert all(len(example.choices) == 4 for example in examples)
    assert {example.source_exam for example in examples} == {
        "47th-TOPIK-II-Reading-Test-Paper_Parse",
        "52nd-TOPIK-II-Reading-Test-Paper_Parse",
        "60th-TOPIK-II-Reading-Test-Paper_Parse",
        "64th-TOPIK-II-Reading-Test-Paper_Parse",
        "83rd-TOPIK-II-Reading-Test-Paper_Parse",
        "91st-TOPIK-II-Reading-Test-Paper_Parse",
        "96th-TOPIK-II-Reading-Test-Paper_Parse",
        "102nd-TOPIK-II-Reading-Test-Paper_Parse",
    }
    assert {example.question_number for example in examples} == {1, 2}

    question_83_2 = next(
        example for example in examples if example.source_exam.startswith("83rd") and example.question_number == 2
    )
    assert question_83_2.choices == ["오곤 한다", "온 모양이다", "오는 편이다", "온 적이 있다"]
    assert question_83_2.answer == 2


def test_inline_choices_are_split():
    examples = scan_extracted_text(ROOT / "extracted_text")
    question_96_1 = next(
        example for example in examples if example.source_exam.startswith("96th") and example.question_number == 1
    )

    assert question_96_1.choices == ["먹느라고", "먹더라도", "먹을 텐데", "먹고 나서"]
    assert question_96_1.stem == "감기약을 ( ) 열이 내렸다."


def test_choices_embedded_inside_blank_are_removed_from_stem():
    examples = scan_extracted_text(ROOT / "extracted_text")
    question_64_1 = next(
        example for example in examples if example.source_exam.startswith("64th") and example.question_number == 1
    )

    assert question_64_1.stem == "나는 주말에는 보통 영화를 ( ) 운동을 한다."
    assert question_64_1.choices == ["보지만", "보거나", "보려고", "보더니"]
