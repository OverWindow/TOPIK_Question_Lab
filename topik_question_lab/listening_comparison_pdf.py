from __future__ import annotations

import io
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from . import comparison_pdf as base_pdf
from .listening_models import ListeningQuestionExample


def _source_text(example: ListeningQuestionExample) -> list[str]:
    lines = [f"{example.source_exam} · {example.question_number}번", "", *example.script_text.splitlines(), "", example.question_prompt]
    choices = example.choices or [option.description for option in example.visual_options]
    lines.extend(f"{index}. {choice}" for index, choice in enumerate(choices, start=1))
    lines.extend(["", f"정답: {example.answer or '미확정'}"])
    return lines


def _generated_text(item: dict[str, Any]) -> list[str]:
    q = item["question"]
    lines = [f"생성 #{item['id']} · {q['type_slot']}번형", ""]
    lines.extend(f"{turn['speaker']}: {turn['text']}" for turn in q.get("dialogue_turns", []))
    lines.extend(["", q.get("question_prompt", "")])
    choices = q.get("choices") or [option.get("description", "") for option in q.get("visual_options", [])]
    lines.extend(f"{index}. {choice}" for index, choice in enumerate(choices, start=1))
    lines.extend(
        f"프롬프트 {option.get('number', index)}: {option.get('image_prompt', '')}"
        for index, option in enumerate(q.get("visual_options", []), start=1)
    )
    lines.extend(["", f"정답: {q['answer']}", f"해설: {q.get('explanation', '')}"])
    return lines


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either serialized data or a Pydantic model.

    Streamlit can reload model modules while the app is running, so an object
    created before a reload is not guaranteed to pass an exact ``isinstance``
    check against the newly imported class even though it has the same fields.
    """
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _draw_visuals(pdf: canvas.Canvas, x: float, y: float, width: float, value: Any) -> float:
    source_page_image = _field(value, "source_page_image", "")
    if source_page_image:
        paths = [Path(source_page_image)] if _field(value, "visual_kind", "none") != "none" else []
    else:
        paths = []
        for option in _field(value, "visual_options", []) or []:
            raw = _field(option, "asset_path", "")
            if raw:
                paths.append(Path(raw))
    paths = [path for path in paths if path.exists()]
    if not paths:
        return y
    if len(paths) == 1:
        image = ImageReader(str(paths[0]))
        image_width, image_height = image.getSize()
        height = min(125, width * image_height / max(1, image_width))
        pdf.drawImage(image, x + 7, y - height, width=width - 14, height=height, preserveAspectRatio=True, anchor="n", mask="auto")
        return y - height - 8
    cell_width = (width - 20) / 2
    cell_height = 72
    for index, path in enumerate(paths[:4]):
        image_x = x + 7 + (index % 2) * (cell_width + 6)
        image_y = y - (index // 2 + 1) * cell_height
        pdf.drawImage(ImageReader(str(path)), image_x, image_y, width=cell_width, height=cell_height - 5, preserveAspectRatio=True, anchor="c", mask="auto")
    return y - cell_height * 2 - 8


def build_listening_comparison_pdf(
    examples: list[ListeningQuestionExample],
    generated: list[dict[str, Any]],
    model_a: tuple[str, str],
    model_b: tuple[str, str],
    title: str,
) -> bytes:
    sources: dict[int, list[ListeningQuestionExample]] = defaultdict(list)
    a_items: dict[int, list[dict]] = defaultdict(list)
    b_items: dict[int, list[dict]] = defaultdict(list)
    for example in examples:
        if example.approved:
            sources[example.question_number].append(example)
    for item in generated:
        identity = (item["provider"], item["model"])
        slot = int(item["question"]["type_slot"])
        if identity == model_a:
            a_items[slot].append(item)
        elif identity == model_b:
            b_items[slot].append(item)
    rows = []
    for slot in sorted(set(sources) & set(a_items) & set(b_items)):
        count = min(len(sources[slot]), len(a_items[slot]), len(b_items[slot]))
        rows.extend((sources[slot][i], a_items[slot][i], b_items[slot][i]) for i in range(count))
    if not rows:
        raise ValueError("비교 가능한 원본·모델 A·모델 B 묶음이 없습니다.")

    base_pdf._register_korean_fonts()
    output = io.BytesIO()
    width, height = landscape(A4)
    pdf = canvas.Canvas(output, pagesize=(width, height), pageCompression=1)
    pdf.setTitle(title)
    margin, gap = 28, 10
    column_width = (width - margin * 2 - gap * 2) / 3
    for source, a_item, b_item in rows:
        pdf.setFont(base_pdf.HEADING_FONT, 13)
        pdf.drawString(margin, height - 25, title)
        headers = ["승인 기출", a_item["model"], b_item["model"]]
        bodies = [_source_text(source), _generated_text(a_item), _generated_text(b_item)]
        visual_values = [source, a_item["question"], b_item["question"]]
        for index, (header, lines, visual_value) in enumerate(zip(headers, bodies, visual_values)):
            x = margin + index * (column_width + gap)
            y = height - 52
            pdf.setFillColor(colors.HexColor("#176b5b"))
            pdf.rect(x, y - 24, column_width, 24, fill=1, stroke=0)
            pdf.setFillColor(colors.white)
            pdf.setFont(base_pdf.HEADING_FONT, 9)
            pdf.drawString(x + 7, y - 16, header[:35])
            y -= 35
            y = _draw_visuals(pdf, x, y, column_width, visual_value)
            pdf.setFillColor(colors.HexColor("#202124"))
            for line in lines:
                for wrapped in base_pdf._wrap_line(line, column_width - 14, base_pdf.BODY_FONT, 8.1):
                    if y < 26:
                        break
                    pdf.setFont(base_pdf.BODY_FONT, 8.1)
                    pdf.drawString(x + 7, y, wrapped)
                    y -= 11
                if y < 26:
                    break
        pdf.showPage()
    pdf.save()
    return output.getvalue()
