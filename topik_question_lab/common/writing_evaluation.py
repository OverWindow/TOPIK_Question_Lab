"""Text-only writing evaluation, independent of Streamlit and database schema changes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from topik_question_lab.common.generation.providers import call_provider
from topik_question_lab.common.models import ProviderResult, utc_now
from topik_question_lab.common.writing_models import WritingReference


OPERATION = "writing_evaluation"
SCORE_LIMITS = {
    51: {"a": 5, "b": 5}, 52: {"a": 5, "b": 5},
    53: {"content": 7, "organization": 7, "language": 16},
    54: {"content": 12, "organization": 12, "language": 26},
}
SCORE_LABELS = {"a": "㉠", "b": "㉡", "content": "내용 및 과제 수행",
                "organization": "글의 전개 구조", "language": "언어 사용"}


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LearnerAnswer(EvaluationModel):
    # Preserve whitespace and wording in the submitted answer and evidence quotes.
    a: str = Field(default="", max_length=2000)
    b: str = Field(default="", max_length=2000)
    text: str = Field(default="", max_length=20000)

    def for_slot(self, slot: int) -> dict[str, str]:
        if slot not in SCORE_LIMITS:
            raise ValueError("51~54번 문항만 평가할 수 있습니다.")
        if (slot < 53 and self.text) or (slot >= 53 and (self.a or self.b)):
            raise ValueError("문항 유형에 맞는 답안 필드를 사용하세요.")
        return {"a": self.a, "b": self.b} if slot < 53 else {"text": self.text}


class AnswerReference(EvaluationModel):
    source_key: str = Field(pattern=r"^\d+:(51|52|53|54)$")
    source_file: str = Field(pattern=r"^\d+_answer\.pdf$")
    source_page: int = Field(ge=1)
    blank_answers: dict[Literal["a", "b"], list[str]] = Field(default_factory=dict)
    text: str = ""
    source_char_count: int | None = Field(default=None, ge=1)
    blank_weights: dict[Literal["a", "b"], dict[Literal["content", "form"], StrictInt]] = Field(default_factory=dict)
    published_rules: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_reference(self):
        exam, slot = map(int, self.source_key.split(":"))
        if self.source_file != f"{exam}_answer.pdf":
            raise ValueError("답안 회차와 출처 파일이 다릅니다.")
        if slot < 53:
            if set(self.blank_answers) != {"a", "b"} or self.text:
                raise ValueError("51·52번에는 두 빈칸의 참고답안이 필요합니다.")
            if any(not items or any(not s.strip() for s in items) for items in self.blank_answers.values()):
                raise ValueError("참고답안은 빈 문자열일 수 없습니다.")
        elif not self.text.strip() or self.blank_answers or self.blank_weights:
            raise ValueError("53·54번에는 서술형 참고답안이 필요합니다.")
        if self.blank_weights:
            if set(self.blank_weights) != {"a", "b"} or not self.published_rules:
                raise ValueError("빈칸 세부 배점에는 출처 기준이 필요합니다.")
            for parts in self.blank_weights.values():
                if set(parts) != {"content", "form"} or min(parts.values()) < 0 or sum(parts.values()) != 5:
                    raise ValueError("빈칸 세부 배점 합계는 5점이어야 합니다.")
        return self


class CriterionScore(EvaluationModel):
    score: int = Field(ge=0, le=26, description="언어 사용은 2배 적용 후 짝수 점수")
    rationale: str = Field(min_length=1)
    quotes: list[str] = Field(default_factory=list, description="학습자 원본 답안에서 그대로 인용")
    parts: dict[Literal["content", "form"], StrictInt] = Field(default_factory=dict,
        description="문항에 출처가 있는 빈칸 세부 배점이 제공된 경우에만 사용")


class TaskCheck(EvaluationModel):
    requirement_id: str = Field(min_length=1)
    status: Literal["충족", "부분 충족", "미충족"]
    reason: str = Field(min_length=1)
    quotes: list[str] = Field(default_factory=list)


class Correction(EvaluationModel):
    original: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EvaluationResult(EvaluationModel):
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    type_slot: Literal[51, 52, 53, 54]
    scores: dict[Literal["a", "b", "content", "organization", "language"], CriterionScore]
    task_checks: list[TaskCheck]
    corrections: list[Correction] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    review_reasons: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(item.score for item in self.scores.values())


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def character_counts(text: str) -> dict:
    # This is a reproducible text count, not a simulation of manuscript-paper cells.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return {
        "공백_포함_줄바꿈_제외": len(normalized.replace("\n", "")),
        "모든_공백_제외": sum(not char.isspace() for char in normalized),
        "계산_방식": "Unicode 코드 포인트 수. 원고지 칸 수·공식 글자 수와 다를 수 있으며 자동 감점에 사용하지 않음.",
    }


def evaluation_tasks(reference: WritingReference) -> dict[str, str]:
    question = reference.question
    if question.type_slot < 53:
        return {"a": "㉠의 문맥·의미·문법·격식", "b": "㉡의 문맥·의미·문법·격식"}
    if question.type_slot == 54:
        return {f"requirement:{i}": value for i, value in enumerate(question.requirements, 1)}
    return {
        **{f"material:{i}": f"자료의 정보와 관계: {value.title}" for i, value in enumerate(question.materials, 1)},
        **{f"note:{i}": value for i, value in enumerate(question.material_notes, 1)},
    }


@dataclass(frozen=True)
class EvaluationRequest:
    system: str
    user: str
    request_id: str

    @property
    def payload(self) -> dict:
        return json.loads(self.user)


def build_evaluation_request(reference: WritingReference, answer_reference: AnswerReference,
                             answer: LearnerAnswer, *, system: str, rubric: dict, notes: str = "") -> EvaluationRequest:
    if reference.source_key != answer_reference.source_key:
        raise ValueError("문항과 참고답안의 출처가 다릅니다.")
    if not system.strip():
        raise ValueError("평가 시스템 프롬프트를 입력하세요.")
    slot = reference.question.type_slot
    original = answer.for_slot(slot)
    payload = {
        "source_key": reference.source_key, "type_slot": slot,
        "question": reference.model_dump(), "answer": original,
        "reference_answer": answer_reference.model_dump(), "rubric": rubric,
        "experimental_notes": notes,
        "tasks": evaluation_tasks(reference),
        "character_counts": {key: character_counts(value) for key, value in original.items()},
        "score_limits": SCORE_LIMITS[slot],
        "output_schema": EvaluationResult.model_json_schema(),
    }
    if slot < 53:
        # Substitute simultaneously so learner text cannot introduce a second replacement.
        import re
        payload["completed_passage"] = re.sub(
            r"\((㉠|㉡)\)", lambda match: original["a" if match[1] == "㉠" else "b"], reference.question.passage)
    request_id = digest({"system": system, "payload": payload})
    payload["request_id"] = request_id
    return EvaluationRequest(system, json.dumps(payload, ensure_ascii=False, indent=2), request_id)


def validate_scores(slot: int, scores: dict[str, int]) -> None:
    limits = SCORE_LIMITS[slot]
    if set(scores) != set(limits):
        raise ValueError("문항 유형에 맞는 평가 영역이 필요합니다.")
    for key, value in scores.items():
        if type(value) is not int or not 0 <= value <= limits[key]:
            raise ValueError(f"{SCORE_LABELS[key]} 점수 범위는 0~{limits[key]}입니다.")
    if slot >= 53 and scores["language"] % 2:
        raise ValueError("2017 기준의 언어 사용 점수는 원점수에 2를 곱한 짝수여야 합니다.")


def validate_evaluation(result: ProviderResult, request: EvaluationRequest) -> EvaluationResult:
    if result.error:
        raise ValueError(result.error)
    parsed = EvaluationResult.model_validate(result.parsed_json)
    payload = request.payload
    slot = payload["type_slot"]
    if parsed.request_id != request.request_id or parsed.type_slot != slot:
        raise ValueError("평가 응답이 현재 문항·답안·프롬프트와 일치하지 않습니다. 현재 요청으로 다시 평가하세요.")
    validate_scores(slot, {key: item.score for key, item in parsed.scores.items()})
    original = payload["answer"]
    texts = list(original.values())
    weights = payload["reference_answer"]["blank_weights"]
    for key, item in parsed.scores.items():
        relevant = [original[key]] if slot < 53 else texts
        if not any(text.strip() for text in relevant) and item.score:
            raise ValueError("미응답에는 점수를 부여할 수 없습니다.")
        if item.score and not item.quotes:
            raise ValueError("점수를 부여한 영역에는 원본 답안 인용이 필요합니다.")
        if any(not quote.strip() or not any(quote in text for text in relevant) for quote in item.quotes):
            raise ValueError("평가 근거 인용이 원본 답안에 없습니다.")
        expected = weights.get(key, {})
        if set(item.parts) != set(expected):
            raise ValueError("출처의 빈칸 세부 배점에 맞는 내용·형식 점수가 필요합니다.")
        if expected and (any(not 0 <= value <= expected[part] for part, value in item.parts.items())
                         or sum(item.parts.values()) != item.score):
            raise ValueError("빈칸 세부 점수의 범위 또는 합계가 맞지 않습니다.")
    task_ids = [check.requirement_id for check in parsed.task_checks]
    if len(task_ids) != len(set(task_ids)) or set(task_ids) != set(payload["tasks"]):
        raise ValueError("모든 과제의 수행 여부를 중복 없이 확인해야 합니다.")
    for check in parsed.task_checks:
        if check.status != "미충족" and not check.quotes:
            raise ValueError("충족한 과제에는 원본 답안 인용이 필요합니다.")
        relevant = [original[check.requirement_id]] if slot < 53 else texts
        if any(not quote.strip() or not any(quote in text for text in relevant) for quote in check.quotes):
            raise ValueError("과제 수행 인용이 원본 답안에 없습니다.")
    for correction in parsed.corrections:
        if not correction.original.strip() or not any(correction.original in text for text in texts):
            raise ValueError("수정 대상으로 인용한 표현이 원본 답안에 없습니다.")
    return parsed


def evaluate_writing(request: EvaluationRequest, *, provider: str, model: str,
                     model_capabilities: dict | None = None, managed_retries: bool | None = None,
                     request_timeout: float | None = None) -> ProviderResult:
    payload = request.payload
    if not any(value.strip() for value in payload["answer"].values()):
        weights = payload["reference_answer"]["blank_weights"]
        parsed = EvaluationResult(
            request_id=request.request_id, type_slot=payload["type_slot"],
            scores={key: CriterionScore(score=0, rationale="미응답", parts={p: 0 for p in weights.get(key, {})})
                    for key in SCORE_LIMITS[payload["type_slot"]]},
            task_checks=[TaskCheck(requirement_id=key, status="미충족", reason="미응답") for key in payload["tasks"]],
            summary="작성된 답안이 없어 0점으로 처리했습니다.")
        return ProviderResult(provider="local", model="empty-answer", operation=OPERATION,
                              raw_response=parsed.model_dump_json(), parsed_json=parsed.model_dump())
    options = {"model_capabilities": model_capabilities}
    if managed_retries is not None:
        options["managed_retries"] = managed_retries
    if request_timeout is not None:
        options["request_timeout"] = request_timeout
    return call_provider(provider, model, OPERATION, request.system, request.user, **options)


def prepare_evaluation_result(result: ProviderResult, request: EvaluationRequest) -> ProviderResult:
    payload = request.payload
    error = ""
    try:
        validate_evaluation(result, request)
    except ValidationError as exc:
        error = " / ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                           for e in exc.errors(include_input=False, include_url=False)[:4])
    except ValueError as exc:
        error = str(exc)
    metadata = {
        **result.request_parameters, "request_id": request.request_id,
        "source_key": payload["source_key"], "rubric_version": payload["rubric"]["version"],
        "prompt_version": digest({"system": request.system, "rubric": payload["rubric"],
                                  "notes": payload["experimental_notes"], "schema": payload["output_schema"]}),
        "answer_id": digest({"question": payload["question"], "answer": payload["answer"]}),
    }
    return result.model_copy(update={"operation": OPERATION, "error": error, "request_parameters": metadata})


def save_evaluation(storage, result: ProviderResult, request: EvaluationRequest) -> dict:
    payload = request.payload
    saved = prepare_evaluation_result(result, request)
    run_id = storage.save_run(saved, request.system, request.user,
                              question_type=f"writing_pbt_{payload['type_slot']}", prompt_mode="evaluation")
    return {"run_id": run_id, "error": saved.error}


def save_human_review(storage, run_id: int, scores: dict[str, int], notes: str) -> None:
    with storage.connect() as connection:
        row = connection.execute("SELECT * FROM runs WHERE id=? AND operation=?", (run_id, OPERATION)).fetchone()
    if row is None:
        raise ValueError("쓰기 평가 실행을 찾을 수 없습니다.")
    result = ProviderResult.model_validate_json(row["result_json"])
    request_payload = json.loads(row["prompt_user"])
    request = EvaluationRequest(row["prompt_system"], row["prompt_user"], request_payload["request_id"])
    evaluation = validate_evaluation(result, request)
    validate_scores(evaluation.type_slot, scores)
    if not notes.strip():
        raise ValueError("사람 채점의 근거를 입력하세요.")
    storage.set_setting(f"writing:evaluation:human:{run_id}", json.dumps({
        "scores": scores, "notes": notes.strip(), "reviewed_at": utc_now(),
        "request_id": request.request_id,
    }, ensure_ascii=False))


def human_review(storage, run_id: int) -> dict:
    return json.loads(storage.get_setting(f"writing:evaluation:human:{run_id}", "{}"))


def comparison_rows(storage, runs: list[dict]) -> list[dict]:
    rows = []
    for run in runs:
        result = json.loads(run["result_json"])
        if result.get("error"):
            continue
        evaluation = EvaluationResult.model_validate(result["parsed_json"])
        human = human_review(storage, run["id"])
        metadata = result["request_parameters"]
        human_total = sum(human["scores"].values()) if human else None
        rows.append({"실행": run["id"], "문항": metadata["source_key"], "서비스": run["provider"], "모델": run["model"],
                     "프롬프트": metadata["prompt_version"][:12], "답안": metadata["answer_id"][:12],
                     "AI 점수": evaluation.total, "사람 점수": human_total,
                     "차이(AI−사람)": evaluation.total - human_total if human_total is not None else None})
    return rows
