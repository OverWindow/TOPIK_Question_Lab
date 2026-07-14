from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


ProviderName = Literal[
    "gpt_5_3_chat",
    "claude",
    "gemini",
    "k_exaone",
    "solar_pro3",
    "llama",
    "gemma",
    "gpt_5_4_nano",
    # Legacy provider IDs remain valid so saved runs can still be loaded.
    "gpt_5_1",
    "gpt_5_2",
    "openai",
    "deepseek",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuestionExample(BaseModel):
    source_exam: str
    question_number: int
    instruction: str
    stem: str
    choices: list[str] = Field(min_length=4, max_length=4)
    answer: int | None = Field(default=None, ge=1, le=4)
    grammar_point: str = ""
    rationale: str = ""
    approved: bool = False
    raw_text: str = ""
    enrichment_model: str = ""
    enrichment_confidence: float | None = Field(default=None, ge=0, le=1)

    @property
    def source_key(self) -> str:
        return f"{self.source_exam}:{self.question_number}"


class TypeAnalysis(BaseModel):
    sentence_structure: str = ""
    tested_grammar: list[str] = Field(default_factory=list)
    difficulty: str = "TOPIK II 읽기 초반 수준"
    answer_conditions: list[str] = Field(default_factory=list)
    distractor_rules: list[str] = Field(default_factory=list)
    notes: str = ""


class GeneratedQuestion(BaseModel):
    type_slot: int = Field(ge=1, le=2)
    stem: str
    choices: list[str] = Field(min_length=4, max_length=4)
    answer: int = Field(ge=1, le=4)
    explanation: str
    target_grammar: str
    difficulty: str = "TOPIK II 읽기 초반"

    @field_validator("stem")
    @classmethod
    def normalize_blank(cls, value: str) -> str:
        return value.replace("（ ）", "( )").replace("(  )", "( )").strip()


class GenerationPayload(BaseModel):
    questions: list[GeneratedQuestion]


class AnalysisPayload(BaseModel):
    analysis: TypeAnalysis


class QuestionEnrichment(BaseModel):
    source_key: str
    answer: int = Field(ge=1, le=4)
    grammar_point: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    confidence: float = Field(default=0.5, ge=0, le=1)


class EnrichmentPayload(BaseModel):
    enrichments: list[QuestionEnrichment]


class ProviderResult(BaseModel):
    provider: ProviderName
    model: str
    operation: Literal["analysis", "enrichment", "generation"]
    raw_response: str = ""
    parsed_json: dict | list | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_seconds: float = 0.0
    error: str = ""
    created_at: str = Field(default_factory=utc_now)


class Review(BaseModel):
    naturalness: int = Field(default=3, ge=1, le=5)
    difficulty_fit: int = Field(default=3, ge=1, le=5)
    distractor_quality: int = Field(default=3, ge=1, le=5)
    topik_fit: int = Field(default=3, ge=1, le=5)
    notes: str = ""
    approved: bool = False


class ValidationIssue(BaseModel):
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
