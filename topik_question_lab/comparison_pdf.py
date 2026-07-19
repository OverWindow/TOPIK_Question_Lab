from __future__ import annotations

import io
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .models import QuestionExample


CIRCLED = ["①", "②", "③", "④"]
BODY_FONT = "TopikKoreanRegular"
HEADING_FONT = "TopikKoreanBold"


@dataclass(frozen=True)
class ComparisonRow:
    source: QuestionExample
    model_a: dict[str, Any]
    model_b: dict[str, Any]


def _register_korean_fonts() -> None:
    if BODY_FONT in pdfmetrics.getRegisteredFontNames():
        return

    home = Path.home()
    candidates = [
        (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf", Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgunbd.ttf"),
        (Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"), Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")),
        (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")),
        (home / "Library/Fonts/NanumGothic.ttf", home / "Library/Fonts/NanumGothicBold.ttf"),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")),
        (Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"), Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf")),
    ]
    for regular_path, bold_path in candidates:
        if not regular_path.exists():
            continue
        resolved_bold = bold_path if bold_path.exists() else regular_path
        pdfmetrics.registerFont(TTFont(BODY_FONT, str(regular_path), subfontIndex=0))
        pdfmetrics.registerFont(TTFont(HEADING_FONT, str(resolved_bold), subfontIndex=0))
        return

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    pdfmetrics.registerFontFamily(
        "TopikKoreanFallback",
        normal="HYSMyeongJo-Medium",
        bold="HYGothic-Medium",
    )
    globals()["BODY_FONT"] = "HYSMyeongJo-Medium"
    globals()["HEADING_FONT"] = "HYGothic-Medium"


def build_comparison_rows(
    examples: list[QuestionExample],
    generated: list[dict[str, Any]],
    model_a: tuple[str, str],
    model_b: tuple[str, str],
    approved_sources_only: bool = True,
    approved_generated_only: bool = False,
) -> list[ComparisonRow]:
    source_by_slot: dict[int, list[QuestionExample]] = defaultdict(list)
    a_by_slot: dict[int, list[dict[str, Any]]] = defaultdict(list)
    b_by_slot: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for example in examples:
        if approved_sources_only and not example.approved:
            continue
        source_by_slot[example.question_number].append(example)
    for slot_examples in source_by_slot.values():
        slot_examples.sort(key=lambda example: (example.source_exam, example.question_number))

    for item in generated:
        if approved_generated_only and not item["review"].get("approved"):
            continue
        identity = (item["provider"], item["model"])
        slot = int(item["question"]["type_slot"])
        if identity == model_a:
            a_by_slot[slot].append(item)
        elif identity == model_b:
            b_by_slot[slot].append(item)
    for grouped in (a_by_slot, b_by_slot):
        for slot_items in grouped.values():
            slot_items.sort(key=lambda item: int(item["id"]))

    rows: list[ComparisonRow] = []
    for slot in sorted(set(source_by_slot) & set(a_by_slot) & set(b_by_slot)):
        complete_count = min(
            len(source_by_slot[slot]),
            len(a_by_slot[slot]),
            len(b_by_slot[slot]),
        )
        for index in range(complete_count):
            rows.append(
                ComparisonRow(
                    source=source_by_slot[slot][index],
                    model_a=a_by_slot[slot][index],
                    model_b=b_by_slot[slot][index],
                )
            )
    return rows


def _question_body(question: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    auxiliary = str(question.get("auxiliary_text", "")).strip()
    if auxiliary:
        lines.extend([f"주어진 문장: {auxiliary}", ""])
    main_text = str(question.get("passage") or question.get("stem") or "").strip()
    highlight = str(question.get("highlight_text", "")).strip()
    if highlight and main_text.count(highlight) == 1:
        main_text = main_text.replace(highlight, f"【{highlight}】", 1)
    if main_text:
        lines.extend([main_text, ""])
    prompt = str(question.get("question_prompt", "")).strip()
    if prompt:
        lines.extend([prompt, ""])
    lines.extend(
        f"{CIRCLED[index]} {choice}"
        for index, choice in enumerate(question.get("choices", []))
    )
    return lines


def _source_lines(example: QuestionExample, include_answers: bool) -> list[tuple[str, str]]:
    question = {
        "stem": example.stem,
        "highlight_text": example.highlight_text,
        "passage": example.passage,
        "question_prompt": example.question_prompt,
        "auxiliary_text": example.auxiliary_text,
        "choices": example.choices,
    }
    lines: list[tuple[str, str]] = [
        (f"{example.source_exam} · {example.question_number}번", "heading"),
        ("", "body"),
        *((line, "body") for line in _question_body(question)),
    ]
    if include_answers:
        answer = str(example.answer) if example.answer is not None else "미확정"
        lines.extend(
            [
                ("", "body"),
                (f"정답: {answer}", "heading"),
                (f"출제 포인트: {example.grammar_point or '-'}", "muted"),
                (f"설명: {example.rationale or '-'}", "muted"),
            ]
        )
    return lines


def _generated_lines(item: dict[str, Any], include_answers: bool) -> list[tuple[str, str]]:
    question = item["question"]
    lines: list[tuple[str, str]] = [
        (f"생성 #{item['id']} · {question['type_slot']}번형", "heading"),
        ("", "body"),
        *((line, "body") for line in _question_body(question)),
    ]
    if include_answers:
        lines.extend(
            [
                ("", "body"),
                (f"정답: {question['answer']}", "heading"),
                (f"출제 포인트: {question.get('target_grammar') or '-'}", "muted"),
                (f"해설: {question.get('explanation') or '-'}", "muted"),
            ]
        )
    return lines


def _wrap_line(text: str, width: float, font_name: str, font_size: float) -> list[str]:
    if not text:
        return [""]
    wrapped: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > width:
                wrapped.append(current.rstrip())
                current = character.lstrip()
            else:
                current = candidate
        wrapped.append(current.rstrip())
    return wrapped or [""]


def _prepare_lines(lines: list[tuple[str, str]], width: float) -> list[tuple[str, str]]:
    prepared: list[tuple[str, str]] = []
    for text, style in lines:
        font_name = HEADING_FONT if style == "heading" else BODY_FONT
        font_size = 9.2 if style == "heading" else 8.3
        prepared.extend(
            (wrapped, style)
            for wrapped in _wrap_line(text, width, font_name, font_size)
        )
    return prepared


def build_comparison_pdf(
    rows: list[ComparisonRow],
    type_label: str,
    model_a_label: str,
    model_b_label: str,
    include_answers: bool = True,
) -> bytes:
    if not rows:
        raise ValueError("비교 가능한 원본·모델 A·모델 B 문제 묶음이 없습니다.")

    _register_korean_fonts()

    output = io.BytesIO()
    page_width, page_height = landscape(A4)
    pdf = canvas.Canvas(output, pagesize=(page_width, page_height), pageCompression=1)
    pdf.setTitle(f"TOPIK 문제 비교 - {type_label}")
    pdf.setAuthor("TOPIK Question Lab")

    margin = 28
    gap = 10
    column_width = (page_width - margin * 2 - gap * 2) / 3
    content_width = column_width - 18
    line_height = 10.8
    lines_per_page = 40
    page_number = 0
    header_labels = ("원본 문제", model_a_label, model_b_label)
    header_fills = (colors.HexColor("#ECEFF1"), colors.HexColor("#E8F0FE"), colors.HexColor("#E6F4EA"))

    for comparison_index, row in enumerate(rows, start=1):
        columns = [
            _prepare_lines(_source_lines(row.source, include_answers), content_width),
            _prepare_lines(_generated_lines(row.model_a, include_answers), content_width),
            _prepare_lines(_generated_lines(row.model_b, include_answers), content_width),
        ]
        continuation_count = max(1, max(math.ceil(len(lines) / lines_per_page) for lines in columns))
        for continuation in range(continuation_count):
            page_number += 1
            pdf.setFillColor(colors.HexColor("#202124"))
            pdf.setFont(HEADING_FONT, 14)
            title_suffix = f" · 계속 {continuation + 1}/{continuation_count}" if continuation_count > 1 else ""
            pdf.drawString(margin, page_height - 27, f"TOPIK 문제 3열 비교 · {type_label}{title_suffix}")
            pdf.setFont(BODY_FONT, 8)
            pdf.setFillColor(colors.HexColor("#5F6368"))
            pdf.drawRightString(page_width - margin, page_height - 26, f"비교 {comparison_index}/{len(rows)} · 페이지 {page_number}")

            for column_index, (header, fill) in enumerate(zip(header_labels, header_fills)):
                x = margin + column_index * (column_width + gap)
                pdf.setFillColor(fill)
                pdf.setStrokeColor(colors.HexColor("#BDC1C6"))
                pdf.roundRect(x, page_height - 66, column_width, 28, 4, fill=1, stroke=1)
                pdf.setFillColor(colors.HexColor("#202124"))
                pdf.setFont(HEADING_FONT, 10)
                pdf.drawString(x + 9, page_height - 55, header)
                pdf.setStrokeColor(colors.HexColor("#DADCE0"))
                pdf.roundRect(x, 34, column_width, page_height - 106, 4, fill=0, stroke=1)

                start = continuation * lines_per_page
                page_lines = columns[column_index][start : start + lines_per_page]
                if continuation > 0 and not page_lines:
                    page_lines = [("(이전 페이지에서 내용 끝)", "muted")]
                y = page_height - 82
                for text, style in page_lines:
                    if style == "heading":
                        pdf.setFont(HEADING_FONT, 9.2)
                        pdf.setFillColor(colors.HexColor("#202124"))
                    elif style == "muted":
                        pdf.setFont(BODY_FONT, 8.0)
                        pdf.setFillColor(colors.HexColor("#5F6368"))
                    else:
                        pdf.setFont(BODY_FONT, 8.3)
                        pdf.setFillColor(colors.HexColor("#303134"))
                    pdf.drawString(x + 9, y, text)
                    y -= line_height

            pdf.setFillColor(colors.HexColor("#80868B"))
            pdf.setFont(BODY_FONT, 7)
            pdf.drawString(margin, 18, "TOPIK Question Lab · 원본과 모델별 생성 결과 비교")
            pdf.showPage()

    pdf.save()
    return output.getvalue()
