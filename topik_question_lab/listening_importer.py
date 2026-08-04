from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .listening_models import DialogueTurn, ListeningQuestionExample, VisualOption
from .listening_profiles import profile_for_number


QUESTION_PAGE_MAP = {
    **{number: number for number in range(1, 4)},
    **{number: 4 for number in range(4, 7)},
    **{number: 5 for number in range(7, 9)},
    **{number: 6 for number in range(9, 11)},
    **{number: 7 for number in range(11, 13)},
    **{number: 8 for number in range(13, 15)},
    **{number: 9 for number in range(15, 17)},
    **{number: 10 for number in range(17, 19)},
    **{number: 11 for number in range(19, 21)},
    **{number: 12 + (number - 21) // 2 for number in range(21, 51)},
}


def exam_name(path: Path) -> str:
    return re.sub(r"-TOPIK-II-Listening-(?:Transcript|Script)$", "", path.stem, flags=re.IGNORECASE)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_listening_pdfs(source_dir: Path) -> list[Path]:
    return sorted(source_dir.glob("*.pdf")) if source_dir.exists() else []


def _page_count(pdf_path: Path) -> int:
    try:
        import pypdfium2 as pdfium

        return len(pdfium.PdfDocument(str(pdf_path)))
    except ImportError:
        from pypdf import PdfReader

        return len(PdfReader(str(pdf_path)).pages)


def render_pdf(pdf_path: Path, import_root: Path, scale: float = 1.7) -> dict:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("PDF 렌더링에는 pypdfium2가 필요합니다. setup 스크립트를 다시 실행하세요.") from exc

    name = exam_name(pdf_path)
    target = import_root / name
    pages_dir = target / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    digest = file_sha256(pdf_path)
    manifest_path = target / "manifest.json"
    existing = {}
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    document = pdfium.PdfDocument(str(pdf_path))
    if existing.get("sha256") != digest or len(list(pages_dir.glob("page-*.png"))) != len(document):
        for page_number, page in enumerate(document, start=1):
            output = pages_dir / f"page-{page_number:02d}.png"
            page.render(scale=scale).to_pil().convert("RGB").save(output, "PNG")
    manifest = {
        "exam": name,
        "source_pdf": str(pdf_path.resolve()),
        "sha256": digest,
        "page_count": len(document),
        "pages": [str((pages_dir / f"page-{number:02d}.png").resolve()) for number in range(1, len(document) + 1)],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    questions_path = target / "questions.json"
    if not questions_path.exists():
        save_imported_examples(target, placeholder_examples(manifest))
    return manifest


def render_reference_pdf(pdf_path: Path, target_root: Path, scale: float = 1.5) -> dict:
    """Render an answer/reference PDF without creating question placeholders."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("PDF 렌더링에는 pypdfium2가 필요합니다. setup 스크립트를 다시 실행하세요.") from exc
    safe_name = re.sub(r"[^0-9A-Za-z가-힣._-]", "_", pdf_path.stem)
    target = target_root / safe_name
    pages_dir = target / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(str(pdf_path))
    digest = file_sha256(pdf_path)
    manifest_path = target / "manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if existing.get("sha256") != digest or len(list(pages_dir.glob("page-*.png"))) != len(document):
        for page_number, page in enumerate(document, start=1):
            page.render(scale=scale).to_pil().convert("RGB").save(pages_dir / f"page-{page_number:02d}.png", "PNG")
    manifest = {
        "name": pdf_path.stem,
        "source_pdf": str(pdf_path.resolve()),
        "sha256": digest,
        "page_count": len(document),
        "pages": [str((pages_dir / f"page-{number:02d}.png").resolve()) for number in range(1, len(document) + 1)],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def placeholder_examples(manifest: dict) -> list[ListeningQuestionExample]:
    values = []
    for number in range(1, 51):
        profile = profile_for_number(number)
        role_index = profile.question_numbers.index(number)
        role = profile.roles[min(role_index, len(profile.roles) - 1)]
        page = QUESTION_PAGE_MAP[number]
        values.append(
            ListeningQuestionExample(
                source_exam=manifest["exam"],
                question_number=number,
                question_type=profile.type_id,
                dialogue_turns=[],
                question_prompt="",
                choices=[],
                repeat_count=profile.repeat_count,
                question_role=role,
                set_key=f"{manifest['exam']}:{min(profile.question_numbers)}-{max(profile.question_numbers)}" if profile.shared_script else "",
                visual_kind=profile.visual_kind,
                source_pdf=manifest["source_pdf"],
                source_page=page,
                source_page_image=manifest["pages"][page - 1],
                parse_warning="",
            )
        )
    return values


def needs_transcription(example: ListeningQuestionExample) -> bool:
    if not example.dialogue_turns or not example.question_prompt.strip():
        return True
    if example.visual_kind == "none":
        return len(example.choices) != 4 or any(not choice.strip() for choice in example.choices)
    return len(example.visual_options) != 4 or any(not option.description.strip() for option in example.visual_options)


def import_directory_for_manifest(manifest: dict) -> Path:
    return Path(manifest["pages"][0]).parent.parent


def save_imported_examples(exam_dir: Path, examples: list[ListeningQuestionExample]) -> None:
    exam_dir.mkdir(parents=True, exist_ok=True)
    output = exam_dir / "questions.json"
    output.write_text(
        json.dumps({"questions": [value.model_dump(mode="json") for value in examples]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_edited_imported_example(import_root: Path, example: ListeningQuestionExample) -> bool:
    """Persist a UI edit to the recognition JSON as well as the type DB."""
    questions_path = import_root / example.source_exam / "questions.json"
    if not questions_path.exists():
        return False
    payload = json.loads(questions_path.read_text(encoding="utf-8"))
    values = [ListeningQuestionExample.model_validate(item) for item in payload.get("questions", [])]
    changed = False
    for index, current in enumerate(values):
        if current.source_key == example.source_key:
            values[index] = example
            changed = True
        elif example.set_key and current.set_key == example.set_key:
            values[index] = current.model_copy(update={"dialogue_turns": example.dialogue_turns})
            changed = True
    if changed:
        save_imported_examples(questions_path.parent, values)
    return changed


def load_imported_examples(import_root: Path) -> list[ListeningQuestionExample]:
    result: list[ListeningQuestionExample] = []
    if not import_root.exists():
        return result
    for path in sorted(import_root.glob("*/questions.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        result.extend(ListeningQuestionExample.model_validate(item) for item in payload.get("questions", []))
    return result


def merge_page_transcription(manifest: dict, payload: dict, model: str) -> list[ListeningQuestionExample]:
    exam_dir = import_directory_for_manifest(manifest)
    existing = {value.question_number: value for value in load_imported_examples(exam_dir.parent) if value.source_exam == manifest["exam"]}
    for raw in payload.get("questions", []):
        number = int(raw.get("question_number", 0))
        if number not in existing:
            continue
        current = existing[number]
        profile = profile_for_number(number)
        turns = [DialogueTurn.model_validate(turn) for turn in raw.get("dialogue_turns", []) if turn.get("speaker") and turn.get("text")]
        raw_visual_options = raw.get("visual_options") or []
        visual_options = [VisualOption.model_validate(option) for option in raw_visual_options]
        discarded_chart_spec = any(
            isinstance(raw_option, dict)
            and raw_option.get("chart_spec") not in (None, "", {})
            and visual_option.chart_spec is None
            for raw_option, visual_option in zip(raw_visual_options, visual_options)
        )
        choices = [str(choice).strip() for choice in raw.get("choices", []) if str(choice).strip()]
        warning = str(raw.get("parse_warning", "")).strip()
        if discarded_chart_spec:
            chart_warning = "불완전한 그래프 명세를 제외했습니다. 생성 프롬프트를 확인하세요."
            warning = f"{warning} / {chart_warning}" if warning else chart_warning
        if len(visual_options) != 4 and profile.visual_kind != "none":
            warning = warning or "시각 선택지 설명 확인 필요"
        if len(choices) != 4 and profile.visual_kind == "none":
            warning = warning or "보기 구조 확인 필요"
        existing[number] = current.model_copy(
            update={
                "instruction": str(raw.get("instruction", "")).strip(),
                "dialogue_turns": turns,
                "question_prompt": str(raw.get("question_prompt", "")).strip(),
                "choices": choices,
                "visual_options": visual_options,
                "parse_warning": warning,
                "transcription_model": model,
                "transcription_confidence": raw.get("confidence"),
            }
        )
    values = [existing[number] for number in sorted(existing)]
    save_imported_examples(exam_dir, values)
    return values


def apply_answer_map(
    examples: list[ListeningQuestionExample],
    answers: dict[int, int],
    source: str = "official",
) -> list[ListeningQuestionExample]:
    result = []
    for example in examples:
        answer = answers.get(example.question_number)
        result.append(example.model_copy(update={"answer": answer, "answer_source": source}) if answer else example)
    return result


def ready_for_enrichment(example: ListeningQuestionExample) -> bool:
    choices = example.choices or [option.description for option in example.visual_options]
    return bool(
        example.answer is None
        and example.dialogue_turns
        and example.question_prompt.strip()
        and len(choices) == 4
        and all(choice.strip() for choice in choices)
    )


def apply_enrichment_payload(
    examples: list[ListeningQuestionExample],
    payload: dict,
    model: str,
) -> tuple[list[ListeningQuestionExample], list[str]]:
    by_key = {example.source_key: example for example in examples}
    applied: dict[str, ListeningQuestionExample] = {}
    for raw in payload.get("enrichments", []):
        source_key = str(raw.get("source_key", ""))
        if source_key not in by_key or source_key in applied:
            continue
        answer = int(raw.get("answer", 0))
        if answer not in {1, 2, 3, 4}:
            continue
        confidence = float(raw.get("confidence", 0.5))
        applied[source_key] = by_key[source_key].model_copy(
            update={
                "answer": answer,
                "answer_source": "ai_suggested",
                "target_skill": str(raw.get("target_skill", "")).strip(),
                "rationale": str(raw.get("rationale", "")).strip(),
                "enrichment_model": model,
                "enrichment_confidence": max(0.0, min(1.0, confidence)),
                "approved": False,
            }
        )
    missing = [example.source_key for example in examples if example.source_key not in applied]
    return [applied.get(example.source_key, example) for example in examples], missing


def validate_import_coverage(examples: list[ListeningQuestionExample]) -> list[str]:
    messages: list[str] = []
    grouped: dict[str, list[int]] = {}
    for example in examples:
        grouped.setdefault(example.source_exam, []).append(example.question_number)
    for exam, numbers in grouped.items():
        missing = sorted(set(range(1, 51)) - set(numbers))
        duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
        if missing:
            messages.append(f"{exam}: 누락 {missing}")
        if duplicates:
            messages.append(f"{exam}: 중복 {duplicates}")
    return messages
