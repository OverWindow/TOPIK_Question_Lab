from __future__ import annotations

import re
from difflib import SequenceMatcher

from .listening_models import GeneratedListeningQuestion, ListeningQuestionExample
from .listening_profiles import listening_type_profile
from .models import ValidationIssue


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


def validate_listening_question(
    question: GeneratedListeningQuestion,
    examples: list[ListeningQuestionExample],
    prior_scripts: list[str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    profile = listening_type_profile(question.question_type)
    if question.type_slot not in profile.question_numbers:
        issues.append(ValidationIssue(code="type_slot", message=f"문항 번호는 {profile.question_numbers} 중 하나여야 합니다."))
    if question.repeat_count != profile.repeat_count:
        issues.append(ValidationIssue(code="repeat_count", message=f"이 유형은 {profile.repeat_count}회 듣기입니다."))
    if not question.dialogue_turns or any(not t.speaker.strip() or not t.text.strip() for t in question.dialogue_turns):
        issues.append(ValidationIssue(code="dialogue", message="화자와 발화가 있는 대본이 필요합니다."))
    if not question.question_prompt.strip():
        issues.append(ValidationIssue(code="question_prompt", message="발문이 없습니다."))
    if profile.shared_script and not question.set_id.strip():
        issues.append(ValidationIssue(code="set_id", message="공통 대본 세트 ID가 없습니다."))
    if profile.visual_kind == "none":
        if len(question.choices) != 4 or any(not c.strip() for c in question.choices):
            issues.append(ValidationIssue(code="choices", message="비어 있지 않은 보기 4개가 필요합니다."))
    else:
        if question.visual_kind != profile.visual_kind:
            issues.append(ValidationIssue(code="visual_kind", message=f"시각 자료 종류는 {profile.visual_kind}여야 합니다."))
        if len(question.visual_options) != 4:
            issues.append(ValidationIssue(code="visual_options", message="시각 선택지 4개가 필요합니다."))
        if any(not v.description.strip() or not v.image_prompt.strip() for v in question.visual_options):
            issues.append(
                ValidationIssue(
                    code="visual_prompt",
                    message="각 시각 선택지의 설명과 그림·그래프 생성 프롬프트가 필요합니다.",
                )
            )
    if len(set(_normalized(c) for c in question.choices if c.strip())) != len([c for c in question.choices if c.strip()]):
        issues.append(ValidationIssue(code="duplicate_choices", message="서로 같은 보기가 있습니다."))

    current = _normalized(question.script_text)
    candidates = [e.script_text for e in examples] + list(prior_scripts or [])
    for candidate in candidates:
        other = _normalized(candidate)
        if current and current == other:
            issues.append(ValidationIssue(code="exact_duplicate", message="기존 대본과 같습니다."))
            break
        if current and other and SequenceMatcher(None, current, other).ratio() >= 0.82:
            issues.append(ValidationIssue(code="similar_duplicate", message="기존 대본과 매우 유사합니다.", severity="warning"))
            break
    return issues


def validate_listening_payload(
    payload: dict,
    examples: list[ListeningQuestionExample],
    question_type: str,
    existing_scripts: list[str] | None = None,
) -> tuple[list[GeneratedListeningQuestion], list[list[ValidationIssue]]]:
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        raise ValueError("JSON에 questions 배열이 없습니다.")
    profile = listening_type_profile(question_type)
    questions: list[GeneratedListeningQuestion] = []
    issues: list[list[ValidationIssue]] = []
    prior = list(existing_scripts or [])
    for raw in raw_questions:
        raw = {**raw, "question_type": question_type}
        question = GeneratedListeningQuestion.model_validate(raw)
        group = validate_listening_question(question, examples, prior)
        questions.append(question)
        issues.append(group)
        if not profile.shared_script:
            prior.append(question.script_text)

    if profile.shared_script:
        grouped: dict[str, list[int]] = {}
        for index, question in enumerate(questions):
            if question.set_id:
                grouped.setdefault(question.set_id, []).append(index)
        for set_id, indexes in grouped.items():
            slots = {questions[i].type_slot for i in indexes}
            scripts = {_normalized(questions[i].script_text) for i in indexes}
            if slots != set(profile.question_numbers):
                issues[indexes[0]].append(ValidationIssue(code="set_slots", message=f"세트 {set_id}에 {profile.question_numbers} 문항이 모두 필요합니다."))
            if len(scripts) != 1:
                issues[indexes[0]].append(ValidationIssue(code="set_script", message=f"세트 {set_id}의 공통 대본이 서로 다릅니다."))
    return questions, issues
