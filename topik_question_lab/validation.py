from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import GeneratedQuestion, QuestionExample, ValidationIssue


def normalize_text(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text).lower()


def validate_question(
    question: GeneratedQuestion,
    examples: list[QuestionExample],
    other_stems: list[str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if question.stem.count("( )") != 1:
        issues.append(ValidationIssue(code="blank_count", message="빈칸 '( )'이 정확히 한 개가 아닙니다."))
    if len(question.choices) != 4 or any(not choice.strip() for choice in question.choices):
        issues.append(ValidationIssue(code="choices", message="비어 있지 않은 보기 4개가 필요합니다."))
    if not 1 <= question.answer <= 4:
        issues.append(ValidationIssue(code="answer", message="정답 번호는 1~4여야 합니다."))
    if not question.explanation.strip():
        issues.append(ValidationIssue(code="explanation", message="정답 해설이 없습니다."))
    if len(set(normalize_text(choice) for choice in question.choices)) != 4:
        issues.append(ValidationIssue(code="duplicate_choices", message="서로 같은 보기가 있습니다."))

    candidates = [example.stem for example in examples] + (other_stems or [])
    normalized = normalize_text(question.stem)
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
) -> tuple[list[GeneratedQuestion], list[list[ValidationIssue]]]:
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        raise ValueError("JSON에 questions 배열이 없습니다.")
    questions: list[GeneratedQuestion] = []
    issues: list[list[ValidationIssue]] = []
    prior_stems: list[str] = list(existing_generated_stems or [])
    for raw_question in raw_questions:
        question = GeneratedQuestion.model_validate(raw_question)
        question_issues = validate_question(question, examples, prior_stems)
        questions.append(question)
        issues.append(question_issues)
        prior_stems.append(question.stem)
    return questions, issues
