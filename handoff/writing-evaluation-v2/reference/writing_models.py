"""PBT writing question formats; no learner answers or scoring models."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True)
class WritingProfile:
    label: str
    points: int
    instruction: str


PROFILES = {
    51: WritingProfile("51번 · 실용문 완성", 10, "다음 글의 ㉠과 ㉡에 알맞은 말을 각각 쓰시오."),
    52: WritingProfile("52번 · 설명문 완성", 10, "다음 글의 ㉠과 ㉡에 알맞은 말을 각각 쓰시오."),
    53: WritingProfile("53번 · 자료 설명", 30, "다음은 ‘{topic}’에 대한 자료이다. 이 내용을 200~300자의 글로 쓰시오. 단, 글의 제목은 쓰지 마시오."),
    54: WritingProfile("54번 · 논술", 50, "다음을 참고하여 600~700자로 글을 쓰시오. 단, 문제를 그대로 옮겨 쓰지 마시오."),
}


class WritingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class ChartSeries(WritingModel):
    label: str = Field(min_length=1)
    values: list[float] = Field(min_length=1)


class WritingMaterial(WritingModel):
    title: str = Field(min_length=1)
    kind: Literal["bar", "line", "table"]
    unit: str = ""
    categories: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list, max_length=6)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_material(self):
        if self.kind == "table":
            if not self.columns or not self.rows or self.categories or self.series:
                raise ValueError("표에는 열 제목과 행을 입력하고 그래프 항목은 비워야 합니다.")
            if any(len(row) != len(self.columns) or any(not cell.strip() for cell in row) for row in self.rows):
                raise ValueError("표의 모든 행은 열 수와 일치하고 빈 셀이 없어야 합니다.")
            labels = self.columns
        else:
            if not self.unit or not self.categories or not self.series or self.columns or self.rows:
                raise ValueError("그래프에는 단위·항목·계열이 필요하며 표 항목은 비워야 합니다.")
            if any(len(s.values) != len(self.categories) for s in self.series):
                raise ValueError("그래프 항목 수와 각 계열의 수치 개수가 일치해야 합니다.")
            if len({s.label for s in self.series}) != len(self.series):
                raise ValueError("그래프 계열 이름은 서로 달라야 합니다.")
            if self.unit == "%" and any(not 0 <= v <= 100 for s in self.series for v in s.values):
                raise ValueError("백분율은 0~100 사이여야 합니다.")
            labels = self.categories
        if any(not label.strip() for label in labels) or len(set(labels)) != len(labels):
            raise ValueError("항목/열 제목은 비어 있거나 중복될 수 없습니다.")
        return self


class WritingQuestion(WritingModel):
    exam_format: Literal["pbt"] = "pbt"
    section: Literal["writing"] = "writing"
    type_slot: Literal[51, 52, 53, 54]
    topic: str = Field(min_length=1)
    instruction: str = ""
    context_title: str = ""
    passage: str = ""
    data_source: str = ""
    materials: list[WritingMaterial] = Field(default_factory=list, max_length=4)
    material_notes: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_question(self):
        canonical = PROFILES[self.type_slot].instruction.format(topic=self.topic)
        if self.instruction and self.instruction != canonical:
            raise ValueError("발문은 쓰기 발문 안내의 표준 발문과 일치해야 합니다.")
        self.instruction = canonical
        if any(not value.strip() for value in self.requirements + self.material_notes):
            raise ValueError("요구 사항과 자료 설명에는 빈 항목을 넣을 수 없습니다.")
        if self.type_slot in (51, 52):
            if any(len(re.findall(r"\(\s*" + marker + r"\s*\)", self.passage)) != 1
                   or self.passage.count(marker) != 1 for marker in ("㉠", "㉡")):
                raise ValueError("지문에 (㉠), (㉡) 빈칸을 각각 한 번 넣어야 합니다.")
            if self.materials or self.material_notes or self.data_source or self.requirements:
                raise ValueError("51·52번에는 도표나 논술 요구 사항을 넣을 수 없습니다.")
        elif self.type_slot == 53:
            if not self.materials:
                raise ValueError("53번에는 그래프 또는 표가 한 개 이상 필요합니다.")
            if self.passage or self.requirements or self.context_title:
                raise ValueError("53번은 지문 대신 구조화된 자료를 사용합니다.")
        else:
            if not self.passage or len(self.requirements) != 3 or len(set(self.requirements)) != 3:
                raise ValueError("54번에는 주제 설명과 서로 다른 요구 사항 세 개가 필요합니다.")
            if self.materials or self.material_notes or self.data_source or self.context_title:
                raise ValueError("54번에는 도표나 실용문 제목을 넣을 수 없습니다.")
        if self.type_slot == 52 and self.context_title:
            raise ValueError("52번에는 실용문 제목을 넣을 수 없습니다.")
        return self


class WritingPayload(WritingModel):
    questions: list[WritingQuestion] = Field(min_length=1, max_length=10)


class WritingReference(WritingModel):
    source_exam: int
    source_file: str = Field(pattern=r"^\d+_writing\.pdf$")
    source_page: Literal[1, 2]
    source_instruction: str = Field(min_length=1)
    question: WritingQuestion

    @property
    def source_key(self) -> str:
        return f"{self.source_exam}:{self.question.type_slot}"
