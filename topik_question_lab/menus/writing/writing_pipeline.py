"""Reference-driven generation and structural checks for PBT writing."""
from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from topik_question_lab.common.generation.providers import call_provider
from topik_question_lab.common.generation.topic_bank import TopicBrief, TopicStore, slot_item_topic_id
from topik_question_lab.common.generation.writing_topic_catalog import WRITING_SLOT_LEVELS, split_writing_topic
from topik_question_lab.common.models import ProviderResult
from topik_question_lab.common.paths import PROJECT_ROOT
from topik_question_lab.common.writing_models import PROFILES, WritingPayload, WritingQuestion, WritingReference
from topik_question_lab.common.writing_storage import WritingStorage
from topik_question_lab.menus.writing.writing_prompts import (
    WritingAnalysis, default_system, prompt_settings, save_prompt_settings,
)


REFERENCE_PATH = Path(__file__).with_name("pbt_references.json")
PDF_ROOT = PROJECT_ROOT / "TOPIK-II-Writing/PBT"


def load_references() -> list[WritingReference]:
    return [WritingReference.model_validate(item) for item in json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))]


def assign_writing_topics(store: TopicStore, slots: list[int], *,
                          rng: random.Random | random.SystemRandom | None = None) -> list[TopicBrief]:
    if not slots or len(slots) > 10 or any(slot not in WRITING_SLOT_LEVELS for slot in slots):
        raise ValueError("51~54번 중 생성할 문항을 1~10개 선택하세요.")
    chooser = rng or random.SystemRandom()
    plan = []
    usage_by_pool = {}
    assigned = Counter()
    for position, slot in enumerate(slots, 1):
        level = WRITING_SLOT_LEVELS[slot]
        pool = store.resolve_source("writing", "writing_pbt", [slot]).pool
        if pool is None:
            raise ValueError(f"{slot}번에 연결된 활성 소재 풀이 없습니다. 쓰기 소재 은행에서 풀을 활성화하세요.")
        candidates = [item for item in pool.items if split_writing_topic(item)[0] == level]
        if not candidates:
            raise ValueError(f"{slot}번에 배정할 {level} 소재가 없습니다. 쓰기 소재 은행에서 ‘{level} > 분야 > 소재’ 형식으로 등록하세요.")
        fresh = [item for item in candidates if not assigned[item]]
        candidates = fresh or candidates
        if pool.id not in usage_by_pool:
            usage_by_pool[pool.id] = store.slot_pool_usage_counts(pool.id)
        counts = {item: usage_by_pool[pool.id][item] + assigned[item] for item in candidates}
        minimum = min(counts.values())
        item = chooser.choice([item for item in candidates if counts[item] == minimum])
        _, category, _ = split_writing_topic(item)
        plan.append(TopicBrief(
            unit_key=f"item-{position}", slot_numbers=[slot],
            topic_id=slot_item_topic_id(pool.id, item), domain=f"{level} > {category}",
            title=item, angle=pool.prompt_instruction, source="slot_pool", pool_id=pool.id,
            prompt_instruction=pool.prompt_instruction,
        ))
        assigned[item] += 1
    return plan


def validate_topic_plan(slots: list[int], plan: list[TopicBrief], store: TopicStore | None = None) -> None:
    if store is not None and len(plan) != len(slots):
        raise ValueError("모든 문항에 소재를 배정한 뒤 생성하세요.")
    units = {f"item-{position}": [slot] for position, slot in enumerate(slots, 1)}
    seen = set()
    for brief in plan:
        if (brief.unit_key in seen or units.get(brief.unit_key) != brief.slot_numbers
                or brief.source != "slot_pool" or not brief.pool_id
                or brief.topic_id != slot_item_topic_id(brief.pool_id, brief.title)):
            raise ValueError("선택 소재와 생성 문항의 순서·연결을 확인하세요.")
        seen.add(brief.unit_key)
        slot = brief.slot_numbers[0]
        if split_writing_topic(brief.title)[0] != WRITING_SLOT_LEVELS.get(slot):
            raise ValueError(f"{slot}번은 {WRITING_SLOT_LEVELS.get(slot, '지정된 수준의')} 소재를 사용해야 합니다.")
        if store is not None:
            pool = store.resolve_source("writing", "writing_pbt", brief.slot_numbers).pool
            if pool is None or pool.id != brief.pool_id or brief.title not in pool.items:
                raise ValueError("선택한 소재의 풀이 변경되었거나 비활성화되었습니다. 소재를 다시 선택하세요.")


def build_prompts(slots: list[int], topic: str, references: list[WritingReference],
                  topic_plan: list[TopicBrief] | None = None, *, settings: dict | None = None) -> tuple[str, str]:
    if not slots or any(slot not in PROFILES for slot in slots) or len(slots) > 10:
        raise ValueError("51~54번 중 생성할 문항을 1~10개 선택하세요.")
    validate_topic_plan(slots, topic_plan or [])
    settings = settings or {}
    system = settings.get("system", default_system(slots))
    relevant = [r for r in references if r.question.type_slot in slots]
    user = json.dumps({
        "생성할_문항번호_순서": slots,
        "유형_분석서": settings.get("analysis_guide", ""),
        "목표_난이도": settings.get("difficulty", ""),
        "유형별_생성_지침": settings.get("instructions", ""),
        "소재_요청": topic.strip() or "배정된 소재를 문항 유형에 맞게 구체화하고 기출과 다른 상황·수치를 사용하십시오.",
        "문항별_선택_소재": [
            {"생성_순서": int(brief.unit_key.removeprefix("item-")), "문항번호": brief.slot_numbers[0],
             **dict(zip(("수준", "분야", "소재"), split_writing_topic(brief.title))),
             "풀_ID": brief.pool_id, "소재_ID": brief.topic_id, "생성_지침": brief.prompt_instruction}
            for brief in (topic_plan or [])
        ],
        "표준_발문": {str(slot): PROFILES[slot].instruction for slot in dict.fromkeys(slots)},
        "발문_주제_치환": "53번 발문의 {topic}은 생성 문항의 topic 값으로 치환하십시오.",
        "참고_기출": [{"출처": r.source_key, "문항": r.question.model_dump()} for r in relevant],
        "출력_JSON_스키마": WritingPayload.model_json_schema(),
    }, ensure_ascii=False, indent=2)
    return system, user


def save_analysis_result(storage: WritingStorage, result: ProviderResult, system: str, user: str,
                         slot: int) -> dict:
    try:
        if result.error:
            raise ValueError(result.error)
        analysis = WritingAnalysis.model_validate(result.parsed_json)
        if analysis.type_slot != slot:
            raise ValueError("선택한 유형과 분석 결과의 문항번호가 다릅니다.")
    except (ValidationError, ValueError) as exc:
        message = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
        run_id = storage.save_run(result.model_copy(update={"error": message}), system, user,
                                  question_type=f"writing_pbt_{slot}")
        return {"run_id": run_id, "error": message}
    run_id = storage.save_run(result, system, user, question_type=f"writing_pbt_{slot}")
    settings = prompt_settings(storage, slot)
    settings["analysis_guide"] = analysis.analysis_guide
    save_prompt_settings(storage, slot, settings)
    return {"run_id": run_id, "error": ""}


def validation_message(exc: ValidationError) -> str:
    return " / ".join(f"{'.'.join(map(str, e['loc'])) or '문항'}: {e['msg']}"
                      for e in exc.errors(include_input=False, include_url=False)[:6])


def save_result(storage: WritingStorage, result: ProviderResult, system: str, user: str,
                slots: list[int], *, topic_plan: list[TopicBrief] | None = None,
                topic_store: TopicStore | None = None) -> dict:
    plan = topic_plan or []
    if plan:
        result = result.model_copy(update={"request_parameters": {
            **result.request_parameters, "writing_topic_plan": [brief.model_dump() for brief in plan],
        }})
    try:
        validate_topic_plan(slots, plan, topic_store)
        if result.error:
            raise ValueError(result.error)
        payload = WritingPayload.model_validate(result.parsed_json)
        actual = [q.type_slot for q in payload.questions]
        if actual != slots:
            raise ValueError(f"요청한 문항 순서·개수 {slots}와 응답 {actual}가 다릅니다.")
        fingerprints = [q.model_dump_json() for q in payload.questions]
        if any(n > 1 for n in Counter(fingerprints).values()):
            raise ValueError("동일한 문항이 중복되었습니다. 서로 다른 문항을 생성하세요.")
    except (ValidationError, ValueError) as exc:
        message = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
        failed = result.model_copy(update={"error": message})
        run_id = storage.save_run(failed, system, user, question_type="writing_pbt")
        return {"run_id": run_id, "question_ids": [], "error": message}
    receipt = storage.save_generation_result(
        result, system, user, "writing_pbt", "standard",
        questions=[q.model_dump() for q in payload.questions], issues=[[] for _ in payload.questions],
    )
    if plan and topic_store is not None:
        try:
            topic_store.record_usages(
                plan, section="writing", question_type="writing_pbt", provider=result.provider,
                model=result.model, run_id=receipt["run_id"],
                usage_key=f"{storage.path.resolve()}:writing_pbt:{receipt['run_id']}",
            )
        except sqlite3.Error:
            # Questions and their topic snapshot have already been committed.
            return {**receipt, "error": "", "warning": "문항은 저장했지만 소재 사용 이력을 기록하지 못했습니다."}
    return {**receipt, "error": ""}


def generate_writing(storage: WritingStorage, *, provider: str, model: str, system: str,
                     user: str, slots: list[int], model_capabilities: dict | None = None,
                     topic_plan: list[TopicBrief] | None = None, topic_store: TopicStore | None = None) -> dict:
    try:
        validate_topic_plan(slots, topic_plan or [], topic_store)
    except ValueError as exc:
        return {"run_id": None, "question_ids": [], "error": str(exc)}
    result = call_provider(provider, model, "generation", system, user, model_capabilities=model_capabilities)
    return save_result(storage, result, system, user, slots, topic_plan=topic_plan, topic_store=topic_store)


def export_json(questions: list[WritingQuestion]) -> str:
    return json.dumps({"questions": [q.model_dump() for q in questions]}, ensure_ascii=False, indent=2)


def export_text(questions: list[WritingQuestion]) -> str:
    blocks = []
    for q in questions:
        lines = [f"{q.type_slot}. {q.instruction} ({PROFILES[q.type_slot].points}점)"]
        lines.extend(value for value in (q.context_title, q.passage, q.data_source) if value)
        for material in q.materials:
            lines.append(f"[{material.title}]" + (f" (단위: {material.unit})" if material.unit else ""))
            if material.kind == "table":
                lines.append(" | ".join(material.columns))
                lines.extend(" | ".join(row) for row in material.rows)
            else:
                lines.append("항목 | " + " | ".join(series.label for series in material.series))
                lines.extend(category + " | " + " | ".join(f"{s.values[i]:g}" for s in material.series)
                             for i, category in enumerate(material.categories))
        lines.extend(f"• {value}" for value in q.material_notes + q.requirements)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
