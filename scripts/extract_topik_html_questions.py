from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "html변환"
OUTPUT_DIR = ROOT / "extracted_text"


QUESTION_RE = re.compile(r"^([1-9]|[1-4][0-9]|50)(?:\.\s*(.*)|)$")


def clean_line(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def element_text(element) -> str:
    if element.name in {"header", "footer"}:
        return ""

    if element.name == "figure":
        img = element.find("img")
        return img.get("alt", "") if img else ""

    if element.name == "table":
        rows: list[str] = []
        for row in element.find_all("tr"):
            cells = [clean_line(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            cells = [cell for cell in cells if cell]
            if cells:
                rows.append(" ".join(cells))
        return "\n".join(rows)

    return element.get_text("\n", strip=True)


def html_blocks(path: Path) -> list[str]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    blocks: list[str] = []
    started = False

    for element in soup.find_all(recursive=False):
        text = element_text(element)
        lines = [clean_line(line) for line in text.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            continue

        block = "\n".join(lines)
        if "TOPIK II 읽기" in block:
            started = True
            continue
        if not started:
            continue

        blocks.append(block)

    return blocks


def split_questions(blocks: list[str]) -> list[dict[str, object]]:
    questions: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    section_instruction = ""
    section_prelude: list[str] = []
    expected = 1

    for block in blocks:
        for raw_line in block.splitlines():
            line = clean_line(raw_line)
            if not line:
                continue
            if line == "7LC20":
                continue

            if line.startswith("※"):
                if current:
                    questions.append(current)
                    current = None
                section_instruction = line
                section_prelude = []
                continue

            match = QUESTION_RE.match(line)
            if match and int(match.group(1)) == expected:
                if current:
                    questions.append(current)
                number = expected
                rest = (match.group(2) or "").strip()
                body: list[str] = []
                if section_instruction:
                    body.append(section_instruction)
                body.extend(section_prelude)
                if rest:
                    body.append(rest)
                current = {"number": number, "lines": body}
                expected += 1
                continue

            if current:
                current["lines"].append(line)  # type: ignore[index]
            elif section_instruction:
                section_prelude.append(line)

    if current:
        questions.append(current)

    return questions


def write_questions(source: Path, questions: list[dict[str, object]]) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    output = OUTPUT_DIR / f"{source.stem}_questions.txt"

    parts = [f"# {source.stem}", ""]
    for question in questions:
        number = question["number"]
        lines = question["lines"]
        parts.append(f"## 문제 {number}")
        parts.extend(lines)  # type: ignore[arg-type]
        parts.append("")

    output.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return output


def main() -> None:
    html_files = sorted(INPUT_DIR.glob("*.html"))
    if not html_files:
        raise SystemExit(f"No HTML files found in {INPUT_DIR}")

    for html_file in html_files:
        blocks = html_blocks(html_file)
        questions = split_questions(blocks)
        output = write_questions(html_file, questions)
        print(f"{html_file.name}: {len(questions)} questions -> {output}")
        if len(questions) != 50:
            found = [str(question["number"]) for question in questions]
            print(f"  WARNING: expected 50 questions, found: {', '.join(found)}")


if __name__ == "__main__":
    main()
