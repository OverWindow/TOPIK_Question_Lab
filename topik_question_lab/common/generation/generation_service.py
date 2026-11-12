"""Generation preparation and validation without Streamlit page state."""
from __future__ import annotations

from collections import Counter
from topik_question_lab.common.generation.exam_profiles import exam_for_type, profile_for, profile_snapshot
from dataclasses import dataclass

from topik_question_lab.common.listening_models import GeneratedListeningQuestion
from topik_question_lab.common.generation.listening_profiles import listening_type_profile
from topik_question_lab.common.generation.listening_prompts import LISTENING_SYSTEM_PROMPT, build_listening_generation_prompt
from topik_question_lab.common.generation.listening_validation import validate_listening_payload
from topik_question_lab.common.models import ValidationIssue
from topik_question_lab.common.generation.prompt_profiles import DEFAULT_PROVIDER_INSTRUCTIONS, apply_provider_instruction, default_analysis_guide, question_type_profile
from topik_question_lab.common.generation.prompts import DEFAULT_SYSTEM_PROMPT, build_generation_prompt
from topik_question_lab.common.generation.providers import generation_preset_prompt
from topik_question_lab.common.generation.topic_bank import apply_topic_plan
from topik_question_lab.common.generation.validation import normalize_text, validate_generation_payload


def default_provider_instruction(provider: str, model: str) -> str:
    if provider in DEFAULT_PROVIDER_INSTRUCTIONS:
        return DEFAULT_PROVIDER_INSTRUCTIONS[provider]
    for marker, key in (("gpt-5.6", "gpt_5_6_luna"), ("gpt", "gpt_5_3_chat"),
                        ("claude", "claude"), ("gemini-3.5", "gemini_3_5_flash"),
                        ("gemini", "gemini"), ("exaone", "k_exaone"),
                        ("solar", "solar_pro3"), ("llama", "llama"), ("gemma", "gemma")):
        if marker in model.lower():
            return DEFAULT_PROVIDER_INSTRUCTIONS[key]
    return "요청된 JSON 구조와 문항 수를 정확히 지키고 JSON 바깥의 설명은 출력하지 마십시오."


def apply_generation_style(storage, section, system, user, provider, model, preset):
    key = f"provider_instruction_{provider}" if section == "reading" else f"instruction_{provider}"
    default = (default_provider_instruction(provider, model) if section == "reading"
               else DEFAULT_PROVIDER_INSTRUCTIONS.get(provider, ""))
    system, user = apply_provider_instruction(
        system, user, provider, storage.get_setting("prompt_mode", "optimized") == "optimized",
        storage.get_setting(key, default),
    )
    return generation_preset_prompt(system, preset), user


def build_generation_snapshot(storage, section, type_id, provider, model, preset, count, topics):
    reading = section == "reading"
    profile = question_type_profile(type_id) if reading else listening_type_profile(type_id)
    examples = storage.list_examples(approved_only=True)
    difficulty = storage.get_setting("difficulty", "TOPIK II 읽기 초반 수준" if reading else "TOPIK II 듣기")
    guide = storage.get_setting("analysis_guide", default_analysis_guide(type_id) if reading else profile.analysis_focus)
    system = storage.get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT if reading else LISTENING_SYSTEM_PROMPT)
    builder = build_generation_prompt if reading else build_listening_generation_prompt
    user = builder(examples, guide, count, difficulty, type_id, topics)
    system, user = apply_generation_style(storage, section, system, user, provider, model, preset)
    return {"exam_format": exam_for_type(type_id), "generation_profile": profile_snapshot(profile),
            "section": section, "type_id": type_id, "slots": list(profile.question_numbers),
            "count": count, "provider": provider, "model": model, "preset": preset,
            "system": system, "user": user, "difficulty": difficulty,
            "prompt_mode": storage.get_setting("prompt_mode", "optimized"),
            "examples": [example.model_dump(mode="json") for example in examples],
            "topics": [brief.model_dump(mode="json") for brief in topics],
            "approved_count": len(examples)}


@dataclass(frozen=True)
class GenerationOutcome:
    ok: bool
    message: str
    run_id: int
    count: int = 0
    review_count: int = 0


def _check_structure(questions, profile, expected_count, shared, reading):
    expected = Counter(profile.question_numbers)
    actual = Counter(question.type_slot for question in questions)
    if len(questions) != expected_count or actual != expected:
        raise ValueError(f"번호별 문항 수 불일치: 필요 {dict(expected)}, 생성 {dict(actual)}")
    if shared:
        ids = {question.set_id for question in questions}
        texts = {normalize_text(question.passage if reading else question.script_text) for question in questions}
        if len(ids) != 1 or not next(iter(ids), "") or len(texts) != 1 or not next(iter(texts), ""):
            raise ValueError("공통 지문·대본과 세트 ID가 모두 동일해야 합니다.")


def record_generation_topics(topic_store, topics, section, type_id, result, run_id, batch_id=None):
    if topic_store and topics:
        topic_store.record_usages(
            topics, section=section, question_type=type_id, provider=result.provider,
            model=result.model, run_id=run_id,
            usage_key=f"{batch_id}:{type_id}" if batch_id else None,
        )


def save_generation(
    storage, result, system, user, *, section, type_id, expected_count,
    topic_store=None, topics=None, strict=False, batch_id=None, examples=None, prompt_mode=None, generation_profile=None,
) -> GenerationOutcome:
    mode = prompt_mode or storage.get_setting("prompt_mode", "optimized")
    reading = section == "reading"
    profile = profile_for(section, type_id, generation_profile)
    shared = profile.shared_passage if reading else profile.shared_script
    topics = topics or []
    try:
        if result.error or result.parsed_json is None:
            raise ValueError(result.error or "JSON 응답이 없습니다.")
        payload = result.parsed_json
        if exam_for_type(type_id) == "ibt":
            payload = {**payload, "questions": [{**q, "exam_format": "ibt", "question_type": type_id,
                       "generation_profile": profile_snapshot(profile)} for q in payload.get("questions", [])]}
        application = apply_topic_plan(payload, topics, shared=shared)
        examples = examples if examples is not None else storage.list_examples(approved_only=True)
        existing = storage.list_generated()
        if reading:
            questions, groups = validate_generation_payload(
                application.payload, examples, [item["question"]["stem"] for item in existing], type_id,
            )
        else:
            scripts = [GeneratedListeningQuestion.model_validate(item["question"]).script_text for item in existing]
            questions, groups = validate_listening_payload(application.payload, examples, type_id, scripts, expected_count)
        for index, messages in enumerate(application.issue_messages):
            if index < len(groups):
                groups[index].extend(ValidationIssue(code="topic_plan", message=message) for message in messages)
        if strict:
            _check_structure(questions, profile, expected_count, shared, reading)
        elif not reading:
            errors = [issue.message for group in groups for issue in group
                      if issue.code == "batch_distribution" and issue.severity == "error"]
            if errors:
                raise ValueError(errors[0])
    except Exception as exc:
        receipt = storage.save_generation_result(result, system, user, type_id, mode, batch_id=batch_id)
        return GenerationOutcome(False, f"응답 검증 실패: {exc}", receipt["run_id"])
    receipt = storage.save_generation_result(
        result, system, user, type_id, mode,
        [question.model_dump(mode="json") for question in questions],
        [[issue.model_dump() for issue in group] for group in groups], batch_id,
    )
    message = f"{len(questions)}문항을 저장했습니다."
    if len(questions) != expected_count:
        message = f"저장했지만 요청한 {expected_count}개 대신 {len(questions)}개가 생성되었습니다."
    try:
        record_generation_topics(topic_store, application.applied_briefs, section, type_id, result, receipt["run_id"], batch_id)
    except Exception as exc:
        message += f" 소재 사용 이력 기록 실패: {exc}"
    return GenerationOutcome(True, message, receipt["run_id"], len(receipt["question_ids"]), receipt["review_count"])
