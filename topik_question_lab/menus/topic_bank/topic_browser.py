"""Exam-facing views of the same topic sources used by generation."""
from dataclasses import dataclass

from topik_question_lab.common.generation.exam_profiles import profiles_for_exam
from topik_question_lab.common.generation.topic_bank import (
    SlotPoolConfigurationError, SlotTopicPool, TopicSource, TopicStore,
    format_slot_numbers, generation_units, topic_reference,
)


@dataclass
class TopicApplication:
    question_type: str
    label: str
    slots: list[int]
    reference_slots: list[int]
    resolved: TopicSource | None
    error: str = ""

    @property
    def name(self) -> str:
        if self.error:
            return "설정 확인 필요"
        if self.resolved.pool:
            return self.resolved.pool.name
        return "공용 소재" if self.resolved.source == "global" else "별도 소재 없음"

    def row(self) -> dict:
        source = self.resolved
        count = len(source.pool.items) if source and source.pool else len(source.topics) if source else 0
        return {"문항": format_slot_numbers(self.slots), "유형": self.label, "적용 풀": self.name, "소재 수": count}


def applications_for_exam(store: TopicStore, section: str, exam_format: str) -> list[TopicApplication]:
    applications = []
    for type_id, profile in profiles_for_exam(section, exam_format).items():
        shared = profile.shared_script if section == "listening" else profile.shared_passage
        for _, slots in generation_units(profile.question_numbers, len(profile.question_numbers), shared):
            _, references = topic_reference(section, type_id, slots)
            try:
                source = store.resolve_source(section, type_id, slots)
                applications.append(TopicApplication(type_id, profile.label, slots, references, source))
            except SlotPoolConfigurationError as exc:
                applications.append(TopicApplication(type_id, profile.label, slots, references, None, str(exc)))
    return sorted(applications, key=lambda item: item.slots[0])


def pool_exam_slots(pool: SlotTopicPool, exam_format: str) -> list[int]:
    slots = []
    for type_id, profile in profiles_for_exam(pool.section, exam_format).items():
        _, references = topic_reference(pool.section, type_id, list(profile.question_numbers))
        slots.extend(number for number, reference in zip(profile.question_numbers, references) if reference in pool.slot_numbers)
    return sorted(set(slots))


def query_number(query: str) -> int | None:
    value = query.strip().removesuffix("번").strip()
    return int(value) if value.isdigit() else None


def application_matches(application: TopicApplication, query: str) -> bool:
    number = query_number(query)
    if number is not None:
        return number in application.slots
    source = application.resolved
    contents = source.pool.items if source and source.pool else [topic.title for topic in source.topics] if source else []
    text = " ".join([format_slot_numbers(application.slots), application.label, application.name, *contents])
    return query.strip().casefold() in text.casefold()
