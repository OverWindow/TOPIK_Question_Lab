"""Strict coverage checks for model review responses."""
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from topik_question_lab.common.vocabulary_models import validate_bundle


def validation_feedback(error, candidate):
    """Keep actionable locations/messages without logging model input values."""
    if not isinstance(error, ValidationError):
        message = str(error) if isinstance(error, ValueError) else "JSON의 필수 항목과 자료형을 확인하세요."
        return [message], []
    questions = candidate.get("questions", []) if isinstance(candidate, dict) else []
    messages, positions, full_bundle = [], set(), False
    for issue in error.errors(include_input=False, include_url=False):
        location = issue["loc"]
        position = None
        if (len(location) >= 2 and location[0] == "questions" and type(location[1]) is int
                and isinstance(questions, list) and location[1] < len(questions)):
            question = questions[location[1]]
            if isinstance(question, dict) and type(question.get("position")) is int and 1 <= question["position"] <= 7:
                position = question["position"]
        if position is None:
            full_bundle = True
        else:
            positions.add(position)
        field = ".".join(str(part) for part in location[2:]) if position is not None else ".".join(map(str, location))
        message = issue["msg"].removeprefix("Value error, ")
        messages.append(f"{str(position) + '번' if position is not None else '묶음'}{(' ' + field) if field else ''}: {message}")
    return messages, [] if full_bundle else sorted(positions)


def validation_retry_details(task):
    """Older checkpoints can be diagnosed without rewriting their history."""
    candidate = task["checkpoint"].get("candidate")
    if task["status"] != "needs_attention" or not isinstance(candidate, dict):
        return None
    try:
        validate_bundle(candidate, task["snapshot"]["word"])
    except ValueError as exc:
        return validation_feedback(exc, candidate)
    return None


class ItemReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(ge=1, le=7, strict=True)
    issues: list[StrictStr]
    warnings: list[StrictStr] = Field(default_factory=list)


class BundleReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues: list[StrictStr]
    questions: list[ItemReview]
    warnings: list[StrictStr] = Field(default_factory=list)


def apply_review(payload, candidate, word):
    review = BundleReview.model_validate(payload)
    positions = [item.position for item in review.questions]
    expected = {question["position"] for question in candidate["questions"]}
    if len(positions) != len(set(positions)) or set(positions) != expected:
        raise ValueError("검수 결과가 모든 문항을 정확히 한 번 포함해야 합니다.")
    if any(not issue.strip() for issue in review.issues + review.warnings
           + [i for q in review.questions for i in q.issues + q.warnings]):
        raise ValueError("검수 오류 사유가 비어 있습니다.")
    issues = list(review.issues)
    warnings = list(review.warnings) + [f"{q.position}번: {message}" for q in review.questions for message in q.warnings]
    failed = []
    for item in review.questions:
        if item.issues:
            failed.append(item.position)
            issues.extend(f"{item.position}번: {issue}" for issue in item.issues)
    if failed == [7] and not review.issues:
        candidate = {**candidate, "questions": [q for q in candidate["questions"] if q["position"] != 7],
                     "skipped_reason": "; ".join(issues)}
        validate_bundle(candidate, word)
        return candidate, {"passed": True, "issues": [], "warnings": warnings, "optional_skipped": issues}, []
    if review.issues:
        failed = sorted(expected)
    return candidate, {"passed": not issues, "issues": issues, "warnings": warnings}, sorted(failed)
