"""Word exercises independent of numbered exam questions."""
from __future__ import annotations

import hashlib
import json
import random
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from topik_question_lab.common.models import normalize_blank_marker


SOURCE_FIELDS = ("vocabulary_id", "korean", "part_of_speech", "english", "vietnamese", "japanese",
                 "chinese_simplified", "indonesian", "example_korean", "example_english", "topik_level")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def source_snapshot(row: dict) -> dict:
    return {key: row.get(key, "") for key in SOURCE_FIELDS}


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def conjugates(word: dict) -> bool:
    pos = normalized(word.get("part_of_speech") or "").lower()
    # Unspecified entries can test usage without requiring conjugation exercises.
    if not pos:
        return False
    if pos in {"동사", "형용사", "verb", "adjective", "v", "v.", "adj", "adj."}:
        return True
    if pos in {"명사", "부사", "관형사", "대명사", "수사", "감탄사", "조사", "의존 명사", "의존명사",
               "noun", "adverb", "pronoun", "determiner", "interjection", "numeral", "particle", "n", "n.", "adv", "adv."}:
        return False
    raise ValueError("품사를 확인한 뒤 생성하세요.")


class VocabularyQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question_type: Literal["sentence_blank", "conjugation", "synonym", "antonym"]
    position: int = Field(ge=1, le=7, strict=True)
    instruction: str = Field(min_length=1)
    stem: str = Field(min_length=1)
    choices: list[str] = Field(min_length=4, max_length=4)
    correct_answer: int = Field(ge=1, le=4, strict=True)
    accepted_answers: list[str] = Field(description=(
        "sentence_blank: required nonempty list including choices[correct_answer - 1] exactly, "
        "plus valid uncontracted alternatives. conjugation/synonym/antonym: exactly []."))
    explanation: str = Field(min_length=1)

    @field_validator("stem")
    @classmethod
    def blank(cls, value):
        return normalize_blank_marker(re.sub(r"_{3,}", "( )", value))

    @field_validator("choices", "accepted_answers")
    @classmethod
    def clean_answers(cls, values):
        result = [normalized(value) for value in values]
        if any(not value for value in result) or len(set(result)) != len(result):
            raise ValueError("보기·허용 답안에는 빈 값이나 중복을 사용할 수 없습니다.")
        return result

    @model_validator(mode="after")
    def consistent(self):
        allowed = {"sentence_blank": range(1, 4), "conjugation": range(4, 7),
                   "synonym": (7,), "antonym": (7,)}
        if self.position not in allowed[self.question_type]:
            raise ValueError("유형과 문항 위치가 맞지 않습니다.")
        if self.question_type == "sentence_blank":
            if self.stem.count("( )") != 1:
                raise ValueError("빈칸은 정확히 하나여야 합니다.")
            if self.choices[self.correct_answer - 1] not in self.accepted_answers:
                raise ValueError("정답 보기를 주관식 허용 답안에 포함하세요.")
            if any(choice in self.accepted_answers for i, choice in enumerate(self.choices, 1)
                   if i != self.correct_answer):
                raise ValueError("오답 보기가 허용 답안과 겹칩니다.")
        elif self.accepted_answers:
            raise ValueError("빈칸 유형만 주관식 허용 답안을 사용합니다.")
        return self


class VocabularyBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_meaning: str = Field(min_length=1)
    questions: list[VocabularyQuestion] = Field(min_length=3, max_length=7)
    skipped_reason: str = ""


class VocabularyRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[VocabularyQuestion] = Field(min_length=1, max_length=7)


def merge_repair(payload, candidate, word, positions):
    original = validate_bundle(candidate, word).model_dump()
    repair = VocabularyRepair.model_validate(payload)
    replacements = {q.position: q for q in repair.questions}
    if len(replacements) != len(repair.questions) or set(replacements) != set(positions):
        raise ValueError("수정 응답은 지적된 문항을 정확히 한 번씩 포함해야 합니다.")
    for index, before in enumerate(original["questions"]):
        if before["position"] not in replacements:
            continue
        after = replacements[before["position"]].model_dump()
        if after["question_type"] != before["question_type"]:
            raise ValueError("수정 과정에서 문항 유형을 바꿀 수 없습니다.")
        # Preserve the balanced answer positions without touching accepted answers.
        answer = after["choices"].pop(after["correct_answer"] - 1)
        after["choices"].insert(before["correct_answer"] - 1, answer)
        after["correct_answer"] = before["correct_answer"]
        original["questions"][index] = after
    return validate_bundle(original, word).model_dump()


def validate_bundle(payload: dict, word: dict) -> VocabularyBundle:
    bundle = VocabularyBundle.model_validate(payload)
    positions = [q.position for q in bundle.questions]
    required = set(range(1, 7) if conjugates(word) else range(1, 4))
    if len(positions) != len(set(positions)) or set(positions) not in (required, required | {7}):
        raise ValueError("품사별 필수 문항 수·위치를 확인하세요.")
    if 7 not in positions and not bundle.skipped_reason:
        raise ValueError("의미 관계 문항을 생략한 이유가 필요합니다.")
    if 7 in positions and bundle.skipped_reason:
        raise ValueError("의미 관계 문항과 생략 이유를 함께 저장할 수 없습니다.")
    stems = [normalized(q.stem) for q in bundle.questions]
    if len(stems) != len(set(stems)):
        raise ValueError("중복 문장이 있습니다.")
    conditions = [normalized(q.instruction + " " + q.stem) for q in bundle.questions if q.question_type == "conjugation"]
    if len(conditions) != len(set(conditions)):
        raise ValueError("활용 조건이 중복됩니다.")
    bundle.questions.sort(key=lambda q: q.position)
    return bundle


def balance_answer_positions(bundle: VocabularyBundle, *, seed: str) -> VocabularyBundle:
    """Copy a validated bundle, preserving answer text and repeatable ordering."""
    rng = random.Random(seed)
    result = bundle.model_copy(deep=True)
    targets = []
    # Balanced prefixes also cover omission of the final optional question.
    for _ in range(0, len(result.questions), 4):
        block = [1, 2, 3, 4]
        rng.shuffle(block)
        targets.extend(block)
    for question, target in zip(result.questions, targets):
        answer = question.choices[question.correct_answer - 1]
        distractors = sorted(choice for choice in question.choices if choice != answer)
        rng.shuffle(distractors)
        distractors.insert(target - 1, answer)
        question.choices = distractors
        question.correct_answer = target
    return result
