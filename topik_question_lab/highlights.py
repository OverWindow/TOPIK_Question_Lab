from __future__ import annotations

import html
import re

from .models import QuestionExample


def has_valid_highlight(example: QuestionExample) -> bool:
    return bool(example.highlight_text and example.stem.count(example.highlight_text) == 1)


def stem_with_highlight_marker(example: QuestionExample) -> str:
    return text_with_highlight_marker(example.stem, example.highlight_text)


def text_with_highlight_marker(text: str, highlight_text: str) -> str:
    if not highlight_text or text.count(highlight_text) != 1:
        return text
    return text.replace(highlight_text, f"[[{highlight_text}]]", 1)


def parse_highlight_marker(stem_value: str, highlight_value: str) -> tuple[str, str, str]:
    matches = re.findall(r"\[\[(.+?)\]\]", stem_value)
    if len(matches) > 1:
        return stem_value, highlight_value, "밑줄 표시는 한 곳에만 지정할 수 있습니다."
    if matches:
        highlight_value = matches[0].strip()
        stem_value = re.sub(r"\[\[(.+?)\]\]", r"\1", stem_value, count=1)
    stem_value = stem_value.strip()
    highlight_value = highlight_value.strip()
    if not highlight_value:
        return stem_value, highlight_value, "밑줄 대상 표현을 지정하세요."
    if stem_value.count(highlight_value) != 1:
        return stem_value, highlight_value, "밑줄 대상 표현이 문장 안에 정확히 한 번 있어야 합니다."
    return stem_value, highlight_value, ""


def highlighted_html(stem: str, highlight_text: str) -> str:
    escaped_stem = html.escape(stem)
    escaped_highlight = html.escape(highlight_text)
    return escaped_stem.replace(escaped_highlight, f"<u>{escaped_highlight}</u>", 1)
