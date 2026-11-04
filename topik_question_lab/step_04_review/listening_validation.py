from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from topik_question_lab.common.listening_models import GeneratedListeningQuestion, ListeningQuestionExample
from topik_question_lab.step_02_planning.listening_profiles import listening_type_profile
from topik_question_lab.common.models import ValidationIssue
from topik_question_lab.step_02_planning.exam_profiles import profile_for, exam_for_type
from topik_question_lab.step_02_planning.topic_bank import generation_units


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


def validate_listening_question(
    question: GeneratedListeningQuestion,
    examples: list[ListeningQuestionExample],
    prior_scripts: list[str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    profile = profile_for("listening", question.question_type, question.generation_profile)
    if question.exam_format != exam_for_type(question.question_type):
        issues.append(ValidationIssue(code="exam_format", message="시험 형식과 문항 유형이 다릅니다."))
    if question.exam_format == "ibt" and question.type_slot in profile.question_numbers:
        role = profile.roles[profile.question_numbers.index(question.type_slot)]
        if question.question_role != role:
            issues.append(ValidationIssue(code="question_role", message=f"질문 역할은 {role}이어야 합니다."))
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
        if question.exam_format == "ibt":
            if sorted(v.number for v in question.visual_options) != [1, 2, 3, 4]:
                issues.append(ValidationIssue(code="visual_options", message="시각 선택지 번호는 중복 없이 1~4여야 합니다."))
            if profile.visual_kind == "chart" and sorted(v.chart_spec.chart_type if v.chart_spec else "" for v in question.visual_options) != ["bar", "bar", "pie", "pie"]:
                issues.append(ValidationIssue(code="chart_spec", message="IBT 그래프 선택지는 원그래프 두 개와 막대그래프 두 개가 필요합니다."))
        if any(not v.description.strip() or not v.image_prompt.strip() for v in question.visual_options):
            issues.append(
                ValidationIssue(
                    code="visual_prompt",
                    message="각 시각 선택지의 설명과 그림·그래프 생성 프롬프트가 필요합니다.",
                )
            )
    if len(set(_normalized(c) for c in question.choices if c.strip())) != len([c for c in question.choices if c.strip()]):
        issues.append(ValidationIssue(code="duplicate_choices", message="서로 같은 보기가 있습니다."))
    topic_values = [question.topic_id, question.topic_domain, question.topic_title, question.topic_angle]
    if any(topic_values) and not all(value.strip() for value in topic_values):
        issues.append(ValidationIssue(code="topic_metadata", message="소재 메타데이터가 일부 누락되었습니다."))

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
    expected_count: int | None = None,
) -> tuple[list[GeneratedListeningQuestion], list[list[ValidationIssue]]]:
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list):
        raise ValueError("JSON에 questions 배열이 없습니다.")
    profile = profile_for("listening", question_type, raw_questions[0].get("generation_profile") if raw_questions else None)
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

    if expected_count is not None:
        expected_units = generation_units(
            profile.question_numbers,
            expected_count,
            profile.shared_script,
        )
        expected_slots = Counter(
            slot for _, unit_slots in expected_units for slot in unit_slots
        )
        actual_slots = Counter(question.type_slot for question in questions)
        if actual_slots != expected_slots:
            expected_text = ", ".join(
                f"{slot}번 {expected_slots[slot]}개" for slot in profile.question_numbers
            )
            actual_text = ", ".join(
                f"{slot}번 {actual_slots[slot]}개"
                for slot in profile.question_numbers
                if actual_slots[slot]
            ) or "없음"
            message = (
                f"요청한 번호별 문항 수와 다릅니다. 필요: {expected_text}; 생성: {actual_text}. "
                "누락되거나 중복된 번호를 보완해 다시 생성하세요."
            )
            if issues:
                issues[0].append(
                    ValidationIssue(code="batch_distribution", message=message)
                )
            else:
                raise ValueError(message)

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
