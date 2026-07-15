from __future__ import annotations

import re
from pathlib import Path

from .models import QuestionExample
from .prompt_profiles import QuestionTypeProfile, question_type_profile


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


def _structure_content(text: str, profile: QuestionTypeProfile) -> tuple[str, str, str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    question_prompt = ""
    for index in range(len(lines) - 1, -1, -1):
        if "고르십시오" in lines[index]:
            question_prompt = lines.pop(index)
            break

    auxiliary_text = ""
    if profile.content_mode == "sentence_insertion" and lines:
        view_index = next(
            (index for index, line in enumerate(lines) if re.fullmatch(r"<\s*보\s*기\s*>", line)),
            None,
        )
        if view_index is not None:
            auxiliary_text = " ".join(lines[view_index + 1 :]).strip()
            lines = lines[:view_index]
        else:
            auxiliary_text = lines.pop(0)

    passage = "\n".join(lines).strip()
    if profile.content_mode == "sentence_insertion":
        position = 0

        def replace_position(_: re.Match) -> str:
            nonlocal position
            position += 1
            return f"({chr(0x2460 + position - 1)})"

        passage = re.sub(r"\(\s*(?:\[c\]\[c\])?\s*\)", replace_position, passage)

    if profile.content_mode == "single_sentence":
        stem = re.sub(r"\s+", " ", passage)
        passage = ""
    else:
        stem_parts = [value for value in (auxiliary_text, passage, question_prompt) if value]
        stem = "\n".join(stem_parts)
    return stem, passage, question_prompt, auxiliary_text


def _parse_section(source_exam: str, number: int, section: str, question_type: str) -> QuestionExample:
    profile = question_type_profile(question_type)
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    instruction = lines[0] if lines and lines[0].startswith("※") else ""
    body_lines = lines[1:] if instruction else lines
    if body_lines and re.fullmatch(r"\(각\s*\d+점\)", body_lines[0]):
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)
    first_choice = re.search(r"[①②③④]", body)
    content_text = body[: first_choice.start()].strip() if first_choice else body
    choice_text = body[first_choice.start():] if first_choice else ""
    stem, passage, question_prompt, auxiliary_text = _structure_content(content_text, profile)
    indexed: dict[int, str] = {}
    for symbol, value in CHOICE_RE.findall(choice_text):
        # Some extracted PDFs place the first two choices inside the printed
        # blank, followed by the rest of the sentence on a new line.
        continuation = re.search(r"(?m)^\s*(\).*)$", value)
        if question_type == "grammar_blank" and stem.count("(") > stem.count(")") and continuation:
            stem = f"{stem} {continuation.group(1).strip()}"
            value = value[: continuation.start()]
        indexed[CHOICE_NUMBER[symbol]] = re.sub(r"\s+", " ", value).strip()

    parse_warning = ""
    if profile.content_mode == "sentence_insertion":
        indexed = {index: chr(0x2460 + index - 1) for index in range(1, 5)}
        position_count = sum(passage.count(f"({chr(0x2460 + index)})") for index in range(4))
        if position_count != 4:
            parse_warning = (
                f"삽입 위치를 {position_count}개만 찾았습니다. "
                "지문에서 (①)~(④) 위치를 직접 복원하세요."
            )
    elif set(indexed) != {1, 2, 3, 4}:
        parse_warning = "추출 원문에서 보기 번호를 완전히 구분하지 못했습니다. 문장과 보기 4개를 확인하세요."

    suggested = INITIAL_ANSWERS.get((source_exam, number))
    answer, grammar, rationale = suggested if suggested else (None, "", "")
    set_key = ""
    if profile.shared_passage:
        set_key = f"{source_exam}:{profile.question_numbers[0]}-{profile.question_numbers[-1]}"
    return QuestionExample(
        source_exam=source_exam,
        question_number=number,
        instruction=instruction,
        stem=stem,
        choices=[indexed.get(index, "") for index in range(1, 5)],
        answer=answer,
        grammar_point=grammar,
        rationale=rationale,
        raw_text=section,
        question_type=question_type,
        passage=passage,
        question_prompt=question_prompt,
        auxiliary_text=auxiliary_text,
        set_key=set_key,
        parse_warning=parse_warning,
    )


def parse_question_file(
    path: Path,
    target_numbers: tuple[int, ...] = (1, 2),
    question_type: str = "grammar_blank",
) -> list[QuestionExample]:
    text = path.read_text(encoding="utf-8")
    source_exam = path.stem.removesuffix("_questions")
    sections = _question_sections(text)
    return [
        _parse_section(source_exam, number, sections[number], question_type)
        for number in target_numbers
        if number in sections
        and not (
            question_type_profile(question_type).skip_undisclosed
            and "NOT disclosed" in sections[number]
        )
    ]


def scan_extracted_text(
    directory: Path,
    target_numbers: tuple[int, ...] = (1, 2),
    question_type: str = "grammar_blank",
) -> list[QuestionExample]:
    examples: list[QuestionExample] = []
    for path in sorted(directory.glob("*_questions.txt")):
        examples.extend(parse_question_file(path, target_numbers, question_type))
    profile = question_type_profile(question_type)
    if profile.shared_passage:
        set_keys = {example.set_key for example in examples if example.set_key}
        for set_key in set_keys:
            siblings = [example for example in examples if example.set_key == set_key]
            shared_passage = max((example.passage for example in siblings), key=len, default="")
            for example in siblings:
                example.passage = shared_passage
                example.stem = "\n".join(
                    value.strip()
                    for value in (example.auxiliary_text, shared_passage, example.question_prompt)
                    if value.strip()
                )
    return examples
