from __future__ import annotations

import io

from pypdf import PdfReader

from topik_question_lab.comparison_pdf import build_comparison_pdf, build_comparison_rows
from topik_question_lab.models import QuestionExample, Review


def generated_item(item_id: int, provider: str, model: str, slot: int, passage: str = "") -> dict:
    stem = passage or "운동을 꾸준히 ( ) 건강이 좋아졌다."
    return {
        "id": item_id,
        "provider": provider,
        "model": model,
        "question": {
            "question_type": "grammar_blank",
            "type_slot": slot,
            "stem": stem,
            "passage": passage,
            "question_prompt": "",
            "auxiliary_text": "",
            "highlight_text": "",
            "choices": ["하면서", "하려면", "하더라도", "하거나"],
            "answer": 1,
            "target_grammar": "-면서",
            "explanation": "동시 동작을 나타낸다.",
            "difficulty": "TOPIK II",
        },
        "review": Review(approved=True).model_dump(),
        "validation": [],
    }


def source_example(slot: int, approved: bool = True) -> QuestionExample:
    return QuestionExample(
        source_exam="102회",
        question_number=slot,
        instruction="",
        stem="운동을 꾸준히 ( ) 건강이 좋아졌다.",
        choices=["하면서", "하려면", "하더라도", "하거나"],
        answer=1,
        grammar_point="-면서",
        rationale="동시 동작을 나타낸다.",
        approved=approved,
    )


def test_comparison_rows_pair_same_slot_and_selected_models():
    generated = [
        generated_item(3, "model_a", "a-1", 1),
        generated_item(1, "model_a", "a-1", 1),
        generated_item(2, "model_b", "b-1", 1),
        generated_item(4, "model_b", "b-1", 2),
    ]

    rows = build_comparison_rows(
        [source_example(1), source_example(2)],
        generated,
        ("model_a", "a-1"),
        ("model_b", "b-1"),
    )

    assert len(rows) == 1
    assert rows[0].source.question_number == 1
    assert rows[0].model_a["id"] == 1
    assert rows[0].model_b["id"] == 2


def test_comparison_pdf_is_landscape_and_splits_long_content():
    long_passage = "한국어 읽기 비교를 위한 긴 지문입니다. " * 180
    rows = build_comparison_rows(
        [source_example(1)],
        [
            generated_item(1, "model_a", "a-1", 1, long_passage),
            generated_item(2, "model_b", "b-1", 1, long_passage),
        ],
        ("model_a", "a-1"),
        ("model_b", "b-1"),
    )

    pdf_data = build_comparison_pdf(rows, "1~2번 문법 빈칸", "모델 A", "모델 B")
    reader = PdfReader(io.BytesIO(pdf_data))

    assert pdf_data.startswith(b"%PDF")
    assert len(reader.pages) > 1
    first_page = reader.pages[0]
    assert float(first_page.mediabox.width) > float(first_page.mediabox.height)
