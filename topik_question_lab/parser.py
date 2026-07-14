from __future__ import annotations

import re
from pathlib import Path

from .models import QuestionExample


QUESTION_HEADING_RE = re.compile(r"^## 문제 (\d+)\s*$", re.MULTILINE)
CHOICE_RE = re.compile(r"([①②③④])\s*(.*?)(?=(?:[①②③④])|$)", re.DOTALL)
CHOICE_NUMBER = {"①": 1, "②": 2, "③": 3, "④": 4}

# Audited suggestions for the currently available source files. They remain unapproved
# until the user confirms them in the UI.
INITIAL_ANSWERS = {
    ("83rd-TOPIK-II-Reading-Test-Paper_Parse", 1): (1, "-(으)면", "조건을 나타내는 연결 표현이 문맥에 맞는다."),
    ("83rd-TOPIK-II-Reading-Test-Paper_Parse", 2): (2, "-(으)ㄴ/는 모양이다", "관찰한 근거로 현재 상황을 추측한다."),
    ("91st-TOPIK-II-Reading-Test-Paper_Parse", 1): (4, "-(으)ㄴ 적이 있다", "과거 경험을 나타낸다."),
    ("91st-TOPIK-II-Reading-Test-Paper_Parse", 2): (4, "-고 나서", "이사 이후 가구를 산 시간 순서를 나타낸다."),
    ("96th-TOPIK-II-Reading-Test-Paper_Parse", 1): (4, "-고 나서", "약을 먹은 뒤 열이 내린 순서를 나타낸다."),
    ("96th-TOPIK-II-Reading-Test-Paper_Parse", 2): (1, "-아/어 놓다", "미래 행동을 위해 미리 준비한 완료 상태를 나타낸다."),
    ("102nd-TOPIK-II-Reading-Test-Paper_Parse", 1): (1, "-(으)ㄴ 지", "어떤 일이 시작된 뒤 흐른 시간을 나타낸다."),
    ("102nd-TOPIK-II-Reading-Test-Paper_Parse", 2): (1, "-아/어 가다", "시간에 따른 점진적인 변화를 나타낸다."),
}


def _question_sections(text: str) -> dict[int, str]:
    matches = list(QUESTION_HEADING_RE.finditer(text))
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[number] = text[match.end():end].strip()
    return sections


def _parse_section(source_exam: str, number: int, section: str) -> QuestionExample:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    instruction = lines[0] if lines and lines[0].startswith("※") else ""
    body = "\n".join(lines[1:] if instruction else lines)
    first_choice = re.search(r"[①②③④]", body)
    if not first_choice:
        raise ValueError(f"{source_exam} 문제 {number}: 보기를 찾을 수 없습니다.")

    stem = body[: first_choice.start()].strip()
    choice_text = body[first_choice.start():]
    indexed: dict[int, str] = {}
    for symbol, value in CHOICE_RE.findall(choice_text):
        # Some extracted PDFs place the first two choices inside the printed
        # blank, followed by the rest of the sentence on a new line.
        continuation = re.search(r"(?m)^\s*(\).*)$", value)
        if stem.count("(") > stem.count(")") and continuation:
            stem = f"{stem} {continuation.group(1).strip()}"
            value = value[: continuation.start()]
        indexed[CHOICE_NUMBER[symbol]] = re.sub(r"\s+", " ", value).strip()
    if set(indexed) != {1, 2, 3, 4}:
        raise ValueError(f"{source_exam} 문제 {number}: 보기 번호가 완전하지 않습니다 ({sorted(indexed)}).")

    suggested = INITIAL_ANSWERS.get((source_exam, number))
    answer, grammar, rationale = suggested if suggested else (None, "", "")
    return QuestionExample(
        source_exam=source_exam,
        question_number=number,
        instruction=instruction,
        stem=re.sub(r"\s+", " ", stem),
        choices=[indexed[index] for index in range(1, 5)],
        answer=answer,
        grammar_point=grammar,
        rationale=rationale,
        raw_text=section,
    )


def parse_question_file(path: Path, target_numbers: tuple[int, ...] = (1, 2)) -> list[QuestionExample]:
    text = path.read_text(encoding="utf-8")
    source_exam = path.stem.removesuffix("_questions")
    sections = _question_sections(text)
    return [_parse_section(source_exam, number, sections[number]) for number in target_numbers if number in sections]


def scan_extracted_text(directory: Path, target_numbers: tuple[int, ...] = (1, 2)) -> list[QuestionExample]:
    examples: list[QuestionExample] = []
    for path in sorted(directory.glob("*_questions.txt")):
        examples.extend(parse_question_file(path, target_numbers))
    return examples
