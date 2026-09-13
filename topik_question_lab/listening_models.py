from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


AnswerSource = Literal["official", "ai_suggested", "manual", "unknown"]
VisualKind = Literal["none", "scene", "chart"]


class DialogueTurn(BaseModel):
    speaker: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ChartSpec(BaseModel):
    chart_type: Literal["bar", "pie", "line"] = "bar"
    title: str = ""
    labels: list[str] = Field(default_factory=list)
    values: list[float] = Field(default_factory=list)
    unit: str = ""

    @field_validator("values")
    @classmethod
    def values_match_labels(cls, value: list[float], info):
        labels = info.data.get("labels", [])
        if labels and len(labels) != len(value):
            raise ValueError("labels와 values의 길이가 같아야 합니다.")
        return value


class VisualOption(BaseModel):
    number: int = Field(ge=1, le=4)
    description: str = ""
    image_prompt: str = ""
    asset_path: str = ""
    chart_spec: ChartSpec | None = None

    @field_validator("chart_spec", mode="before")
    @classmethod
    def discard_invalid_optional_chart_spec(cls, value):
        if value in (None, "", {}):
            return None
        try:
            return ChartSpec.model_validate(value)
        except (ValidationError, TypeError, ValueError):
            # A chart specification is optional. AI transcription sometimes
            # returns a partial object such as {"values": null}; keep the
            # visual description/prompt and let the user approve without it.
            return None


class ListeningQuestionExample(BaseModel):
    source_exam: str
    question_number: int = Field(ge=1, le=50)
    question_type: str
    instruction: str = ""
    dialogue_turns: list[DialogueTurn] = Field(default_factory=list)
    question_prompt: str = ""
    choices: list[str] = Field(default_factory=list)
    answer: int | None = Field(default=None, ge=1, le=4)
    answer_source: AnswerSource = "unknown"
    target_skill: str = ""
    rationale: str = ""
    repeat_count: int = Field(default=1, ge=1, le=2)
    question_role: str = ""
    set_key: str = ""
    visual_kind: VisualKind = "none"
    visual_options: list[VisualOption] = Field(default_factory=list)
    approved: bool = False
    source_pdf: str = ""
    source_page: int = Field(default=0, ge=0)
    source_page_image: str = ""
    parse_warning: str = ""
    transcription_model: str = ""
    transcription_confidence: float | None = Field(default=None, ge=0, le=1)
    enrichment_model: str = ""
    enrichment_confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("parse_warning", mode="before")
    @classmethod
    def clear_legacy_transcription_status(cls, value):
        # "전사 필요"는 사용자 메모가 아니라 작업 상태였으므로 화면의
        # 기본 메모로 노출하지 않는다. 전사 상태는 실제 필드로 판정한다.
        return "" if str(value or "").strip() == "전사 필요" else str(value or "")

    @property
    def source_key(self) -> str:
        return f"{self.source_exam}:{self.question_number}"

    @property
    def script_text(self) -> str:
        return "\n".join(f"{turn.speaker}: {turn.text}" for turn in self.dialogue_turns)


class GeneratedListeningQuestion(BaseModel):
    question_type: str
    type_slot: int = Field(ge=1, le=50)
    dialogue_turns: list[DialogueTurn] = Field(min_length=1)
    question_prompt: str = Field(min_length=1)
    choices: list[str] = Field(default_factory=list)
    answer: int = Field(ge=1, le=4)
    explanation: str = Field(min_length=1)
    target_skill: str = Field(min_length=1)
    difficulty: str = "TOPIK II 듣기"
    repeat_count: int = Field(default=1, ge=1, le=2)
    question_role: str = ""
    set_id: str = ""
    visual_kind: VisualKind = "none"
    visual_options: list[VisualOption] = Field(default_factory=list)
    answer_source: AnswerSource = "ai_suggested"
    topic_id: str = ""
    topic_domain: str = ""
    topic_title: str = ""
    topic_angle: str = ""

    @property
    def script_text(self) -> str:
        return "\n".join(f"{turn.speaker}: {turn.text}" for turn in self.dialogue_turns)


class ListeningGenerationPayload(BaseModel):
    questions: list[GeneratedListeningQuestion]
