from __future__ import annotations

import re
from difflib import SequenceMatcher

from topik_question_lab.common.models import GeneratedQuestion, QuestionExample, ValidationIssue, normalize_blank_marker
from topik_question_lab.common.generation.prompt_profiles import question_type_profile
from topik_question_lab.common.generation.exam_profiles import profile_for, exam_for_type


CIRCLED_NUMBER_TRANSLATION = str.maketrans(
    {"①": "1", "②": "2", "③": "3", "④": "4"}
)
BLANK_SLOTS = {
    "paragraph_blank_short": {16, 17, 18},
    "paragraph_blank": {28, 29, 30, 31},
    "paired_19_20": {19},
    "paired_21_22": {21},
    "paired_44_45": {44},
    "paired_48_50": {49},
}


def blank_slots(type_id, snapshot=None):
    if not type_id.startswith("ibt_"):
        return BLANK_SLOTS.get(type_id, set())
    profile = profile_for("reading", type_id, snapshot)
    return {slot for slot, role in zip(profile.question_numbers, profile.roles) if "빈칸" in role}


def base_type(type_id, snapshot=None):
    return profile_for("reading", type_id, snapshot).base_type_id if type_id.startswith("ibt_") else type_id


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text.translate(CIRCLED_NUMBER_TRANSLATION)).lower()


def choices_are_distinct(choices: list[str]) -> bool:
    return len(choices) == 4 and len({normalize_text(choice) for choice in choices}) == 4


def discard_resolved_validation_issues(question: dict, issues: list[dict]) -> list[dict]:
    """Hide legacy duplicate-choice errors that the current validator resolves."""
    if (question.get("generation_profile") or {}).get("section") == "listening":
        return issues
    choices = [str(choice) for choice in question.get("choices", [])]
    resolved_codes: set[str] = set()
    if choices_are_distinct(choices):
        resolved_codes.add("duplicate_choices")
    question_type = str(question.get("question_type", ""))
    slot = int(question.get("type_slot", 0) or 0)
    if base_type(question_type, question.get("generation_profile")) == "grammar_blank" and normalize_blank_marker(str(question.get("stem", ""))).count("( )") == 1:
        resolved_codes.add("blank_count")
    elif slot in blank_slots(question_type, question.get("generation_profile")) and normalize_blank_marker(str(question.get("passage", ""))).count("( )") == 1:
        resolved_codes.add("blank_count")
    return [issue for issue in issues if issue.get("code") not in resolved_codes]


def normalize_question_blank_markers(question: dict) -> dict:
    normalized = dict(question)
    question_type = str(normalized.get("question_type", ""))
    slot = int(normalized.get("type_slot", 0) or 0)
    if base_type(question_type, question.get("generation_profile")) == "grammar_blank":
        normalized["stem"] = normalize_blank_marker(str(normalized.get("stem", "")))
    elif slot in blank_slots(question_type, question.get("generation_profile")):
        normalized["passage"] = normalize_blank_marker(str(normalized.get("passage", "")))
        normalized["stem"] = normalize_blank_marker(str(normalized.get("stem", "")))
    return normalized


def validate_question(
    question: GeneratedQuestion,
    examples: list[QuestionExample],
    other_stems: list[str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    profile = profile_for("reading", question.question_type, question.generation_profile)
    semantic_type = getattr(profile, "base_type_id", question.question_type)
    if question.exam_format != exam_for_type(question.question_type):
        issues.append(ValidationIssue(code="exam_format", message="시험 형식과 문항 유형이 다릅니다."))
    material_kind = getattr(profile, "material_kind", "none")
    if material_kind != "none":
        material = question.visual_material or {}
        try:
            if material_kind == "material_banner":
                from topik_question_lab.common.media.listening_visuals import render_banner
                if material.get("text", "").strip() != question.passage.strip():
                    raise ValueError("광고·표어 문구와 지문이 일치해야 합니다.")
                render_banner(material.get("text", ""))
            else:
                from topik_question_lab.common.media.media_generation import chart_spec
                spec = chart_spec(material.get("chart_spec"), question.passage)
                if spec["chart_type"] != "combo":
                    raise ValueError("IBT 읽기 7번은 막대와 꺾은선의 복합 도표가 필요합니다.")
        except (ValueError, TypeError) as exc:
            issues.append(ValidationIssue(code="visual_material", message=f"시각 자료 확인 필요: {exc}"))
    if profile.question_numbers and question.type_slot not in profile.question_numbers:
        issues.append(
            ValidationIssue(
                code="type_slot",
                message=f"문제 번호 슬롯은 {profile.question_numbers} 중 하나여야 합니다.",
            )
        )
    if question.type_slot in profile.highlight_numbers:
        if not question.highlight_text.strip():
            issues.append(ValidationIssue(code="highlight_missing", message="밑줄 대상 표현이 없습니다."))
        elif question.stem.count(question.highlight_text) != 1:
            issues.append(
                ValidationIssue(code="highlight_match", message="밑줄 대상 표현이 지문에 정확히 한 번 있어야 합니다.")
            )
    if semantic_type == "similar_expression":
        if "( )" in question.stem:
            issues.append(ValidationIssue(code="unexpected_blank", message="유사 표현 유형에는 빈칸을 사용하지 않습니다."))
    elif semantic_type == "grammar_blank" and normalize_blank_marker(question.stem).count("( )") != 1:
        issues.append(ValidationIssue(code="blank_count", message="빈칸 '( )'이 정확히 한 개가 아닙니다."))
    elif semantic_type not in {"grammar_blank", "similar_expression"}:
        if not question.passage.strip():
            issues.append(ValidationIssue(code="passage", message="지문·자료가 없습니다."))
        if question.type_slot in blank_slots(question.question_type, question.generation_profile):
            if normalize_blank_marker(question.passage).count("( )") != 1:
                issues.append(
                    ValidationIssue(code="blank_count", message="이 문항의 지문에는 빈칸 '( )'이 정확히 하나 필요합니다.")
                )
        if profile.content_mode == "sentence_order":
            missing = [marker for marker in ("(가)", "(나)", "(다)", "(라)") if marker not in question.passage]
            if missing:
                issues.append(
                    ValidationIssue(code="order_markers", message=f"문장 배열 표지가 부족합니다: {', '.join(missing)}")
                )
        if profile.content_mode == "sentence_insertion":
            missing = [f"({chr(0x2460 + index)})" for index in range(4) if f"({chr(0x2460 + index)})" not in question.passage]
            if missing:
                issues.append(
                    ValidationIssue(code="insertion_markers", message=f"삽입 위치 표지가 부족합니다: {', '.join(missing)}")
                )
            if not question.auxiliary_text.strip():
                issues.append(ValidationIssue(code="auxiliary_text", message="삽입할 문장이 없습니다."))
        if profile.shared_passage and not question.set_id.strip():
            issues.append(ValidationIssue(code="set_id", message="공통 지문 세트 ID가 없습니다."))
    if semantic_type == "paired_48_50" and question.exam_format == "ibt" and "( )" in normalize_blank_marker(question.passage):
        issues.append(ValidationIssue(code="unexpected_blank", message="목적·내용 지문에는 빈칸을 넣지 않습니다."))
    if len(question.choices) != 4 or any(not choice.strip() for choice in question.choices):
        issues.append(ValidationIssue(code="choices", message="비어 있지 않은 보기 4개가 필요합니다."))
    if not 1 <= question.answer <= 4:
        issues.append(ValidationIssue(code="answer", message="정답 번호는 1~4여야 합니다."))
    if not question.explanation.strip():
        issues.append(ValidationIssue(code="explanation", message="정답 해설이 없습니다."))
    if not choices_are_distinct(question.choices):
        issues.append(ValidationIssue(code="duplicate_choices", message="서로 같은 보기가 있습니다."))
    topic_values = [question.topic_id, question.topic_domain, question.topic_title, question.topic_angle]
    if any(topic_values) and not all(value.strip() for value in topic_values):
        issues.append(ValidationIssue(code="topic_metadata", message="소재 메타데이터가 일부 누락되었습니다."))

    candidates = [(example.passage or example.stem) for example in examples] + (other_stems or [])
    question_text = question.passage or question.stem
    normalized = normalize_text(question_text)
    for candidate in candidates:
        candidate_normalized = normalize_text(candidate)
        if normalized == candidate_normalized:
            issues.append(ValidationIssue(code="exact_duplicate", message="기존 문장과 같습니다."))
            break
        if normalized and SequenceMatcher(None, normalized, candidate_normalized).ratio() >= 0.82:
            issues.append(
                ValidationIssue(code="similar_duplicate", message="기존 문장과 매우 유사합니다.", severity="warning")
            )
            break
    return issues


def validate_generation_payload(
    payload: dict,
    examples: list[QuestionExample],
    existing_generated_stems: list[str] | None = None,
    question_type: str = "grammar_blank",
) -> tuple[list[GeneratedQuestion], list[list[ValidationIssue]]]:
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        raise ValueError("JSON에 questions 배열이 없습니다.")
    questions: list[GeneratedQuestion] = []
    issues: list[list[ValidationIssue]] = []
    prior_stems: list[str] = list(existing_generated_stems or [])
    profile = None
    for raw_question in raw_questions:
        question = GeneratedQuestion.model_validate(raw_question)
        if question.question_type != question_type:
            question = question.model_copy(update={"question_type": question_type})
        profile = profile_for("reading", question_type, question.generation_profile)
        question_issues = validate_question(question, examples, prior_stems)
        questions.append(question)
        issues.append(question_issues)
        if not profile.shared_passage:
            prior_stems.append(question.passage or question.stem)
    if profile and profile.shared_passage:
        grouped: dict[str, list[int]] = {}
        passages: dict[str, set[str]] = {}
        for index, question in enumerate(questions):
            if not question.set_id:
                continue
            grouped.setdefault(question.set_id, []).append(index)
            passages.setdefault(question.set_id, set()).add(normalize_text(question.passage))
        expected_slots = set(profile.question_numbers)
        for set_id, indexes in grouped.items():
            actual_slots = {questions[index].type_slot for index in indexes}
            messages = []
            if actual_slots != expected_slots:
                messages.append(f"세트 {set_id}의 문항 슬롯이 {sorted(expected_slots)}와 일치하지 않습니다.")
            if len(passages[set_id]) != 1:
                messages.append(f"세트 {set_id}의 공통 지문이 서로 다릅니다.")
            for message in messages:
                issues[indexes[0]].append(ValidationIssue(code="set_structure", message=message))
    return questions, issues
