from pathlib import Path

import pytest

from topik_question_lab.parser import scan_extracted_text
from topik_question_lab.prompt_profiles import QUESTION_TYPE_PROFILES


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


def test_scans_similar_expression_type_separately():
    examples = scan_extracted_text(ROOT / "extracted_text", (3, 4), "similar_expression")

    assert len(examples) == 16
    assert {example.question_number for example in examples} == {3, 4}
    assert all(example.question_type == "similar_expression" for example in examples)
    assert all(example.highlight_text == "" for example in examples)

    question_47_3 = next(
        example for example in examples if example.source_exam.startswith("47th") and example.question_number == 3
    )
    assert question_47_3.choices == ["일어난 탓에", "일어난 김에", "일어나는 대신", "일어나는 대로"]


@pytest.mark.parametrize(
    ("type_id", "expected_count"),
    [
        ("grammar_blank", 16),
        ("similar_expression", 16),
        ("short_text_topic", 32),
        ("content_match_short", 32),
        ("sentence_order", 24),
        ("paragraph_blank_short", 24),
        ("paired_19_20", 16),
        ("paired_21_22", 16),
        ("headline_interpretation", 24),
        ("paragraph_blank", 28),
        ("content_match", 21),
        ("main_topic", 27),
        ("sentence_insertion", 18),
        ("paired_42_43", 8),
        ("paired_44_45", 12),
        ("paired_46_47", 12),
        ("paired_48_50", 18),
    ],
)
def test_scans_every_supported_type(type_id, expected_count):
    profile = QUESTION_TYPE_PROFILES[type_id]
    examples = scan_extracted_text(ROOT / "extracted_text", profile.question_numbers, type_id)

    assert len(examples) == expected_count
    assert all(example.question_type == type_id for example in examples)
    assert all(example.question_number in profile.question_numbers for example in examples)
    assert all(example.question_number not in {23, 24} for example in examples)


def test_partial_visual_choices_are_kept_for_manual_repair():
    profile = QUESTION_TYPE_PROFILES["short_text_topic"]
    examples = scan_extracted_text(ROOT / "extracted_text", profile.question_numbers, profile.type_id)

    assert any(example.parse_warning for example in examples)
    assert all(len(example.choices) == 4 for example in examples)


def test_sentence_insertion_extracts_given_sentence_and_positions():
    profile = QUESTION_TYPE_PROFILES["sentence_insertion"]
    examples = scan_extracted_text(ROOT / "extracted_text", profile.question_numbers, profile.type_id)
    assert all(example.auxiliary_text for example in examples)
    assert all(example.choices == ["①", "②", "③", "④"] for example in examples)
    assert all(
        all(marker in example.passage for marker in ("(①)", "(②)", "(③)", "(④)"))
        or example.parse_warning
        for example in examples
    )

    older_example = next(
        example
        for example in examples
        if example.source_exam.startswith("52nd") and example.question_number == 39
    )
    assert older_example.auxiliary_text.startswith("악취에 동일한 양의")
    assert all(marker in older_example.passage for marker in ("(①)", "(②)", "(③)", "(④)"))


def test_shared_passage_examples_receive_a_set_key():
    profile = QUESTION_TYPE_PROFILES["paired_48_50"]
    examples = scan_extracted_text(ROOT / "extracted_text", profile.question_numbers, profile.type_id)

    assert all(example.set_key for example in examples)
    assert len({example.set_key for example in examples}) == len(examples) // 3


def test_every_shared_set_uses_one_normalized_passage():
    for type_id, profile in QUESTION_TYPE_PROFILES.items():
        if not profile.shared_passage:
            continue
        examples = scan_extracted_text(ROOT / "extracted_text", profile.question_numbers, type_id)
        for set_key in {example.set_key for example in examples}:
            assert len({example.passage for example in examples if example.set_key == set_key}) == 1
