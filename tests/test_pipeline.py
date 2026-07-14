import json

from topik_question_lab.exports import to_csv, to_json, to_txt
from topik_question_lab.models import EnrichmentPayload, GeneratedQuestion, QuestionExample, Review
from topik_question_lab.prompts import build_enrichment_prompt
from topik_question_lab.providers import (
    DEFAULT_PROVIDERS,
    call_provider,
    can_gateway_call,
    extract_json,
    has_api_key,
    list_chatkhu_models,
    manual_result,
    provider_label,
)
from topik_question_lab.storage import Storage
from topik_question_lab.validation import validate_generation_payload, validate_question


def sample_question(stem: str = "비가 그친 후에 공원에 ( ).") -> dict:
    return {
        "type_slot": 2,
        "stem": stem,
        "choices": ["가기로 했다", "가고 말았다", "갈 뻔했다", "가는 편이다"],
        "answer": 1,
        "explanation": "미래 계획이므로 1번이 알맞다.",
        "target_grammar": "-기로 하다",
        "difficulty": "TOPIK II 읽기 초반 수준",
    }


def test_extract_json_accepts_code_fence_and_surrounding_text():
    assert extract_json('```json\n{"questions": []}\n```') == {"questions": []}
    assert extract_json('응답입니다. {"analysis": {"notes": "ok"}} 끝') == {"analysis": {"notes": "ok"}}


def test_manual_invalid_json_preserves_raw_response():
    result = manual_result("openai", "test-model", "generation", "not-json")

    assert result.raw_response == "not-json"
    assert result.parsed_json is None
    assert result.error


def test_enrichment_prompt_and_response_round_trip():
    example = QuestionExample(
        source_exam="sample",
        question_number=1,
        instruction="",
        stem="운동을 꾸준히 ( ) 건강이 좋아졌다.",
        choices=["하면서", "하려면", "하더라도", "하거나"],
    )
    prompt = build_enrichment_prompt([example])
    raw = json.dumps(
        {
            "enrichments": [
                {
                    "source_key": example.source_key,
                    "answer": 1,
                    "grammar_point": "-(으)면서",
                    "rationale": "두 행동의 동시 진행을 나타내므로 1번이 적절하다.",
                    "confidence": 0.91,
                }
            ]
        },
        ensure_ascii=False,
    )
    result = manual_result("k_exaone", "test-model", "enrichment", raw)
    payload = EnrichmentPayload.model_validate(result.parsed_json)

    assert example.source_key in prompt
    assert payload.enrichments[0].answer == 1
    assert payload.enrichments[0].confidence == 0.91


def test_chatkhu_without_key_stays_in_manual_mode(monkeypatch):
    monkeypatch.delenv("CHATKHU_API_KEY", raising=False)

    assert has_api_key("openai") is False
    result = call_provider("openai", "test-model", "analysis", "system", "user")
    assert "ChatKHU 웹 수동 모드" in result.error

    try:
        list_chatkhu_models()
    except ValueError as exc:
        assert "CHATKHU_API_KEY" in str(exc)
    else:
        raise AssertionError("키가 없으면 모델 목록을 요청하면 안 됩니다.")


def test_active_model_comparison_set(monkeypatch):
    monkeypatch.setenv("CHATKHU_API_KEY", "test-key")

    assert list(DEFAULT_PROVIDERS) == [
        "gpt_5_3_chat",
        "claude",
        "gemini",
        "k_exaone",
        "solar_pro3",
        "llama",
        "gemma",
        "gpt_5_4_nano",
    ]
    assert "deepseek" not in DEFAULT_PROVIDERS
    assert DEFAULT_PROVIDERS["gpt_5_3_chat"].model == "gpt-5.3-chat-latest"
    assert DEFAULT_PROVIDERS["gemini"].model == "gemini-3.1-flash-lite"
    assert DEFAULT_PROVIDERS["k_exaone"].model == "LGAI-EXAONE/K-EXAONE-236B-A23B"
    assert DEFAULT_PROVIDERS["solar_pro3"].model == "solar-pro3"
    assert DEFAULT_PROVIDERS["llama"].model == "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
    assert DEFAULT_PROVIDERS["gemma"].model == "google/gemma-3-27b-it"
    assert DEFAULT_PROVIDERS["gpt_5_4_nano"].model == "gpt-5.4-nano"
    assert can_gateway_call("k_exaone") is True
    assert provider_label("gpt_5_1") == "GPT 5.1 (기존 기록)"
    assert provider_label("deepseek") == "DeepSeek (기존 기록)"


def test_generation_validation_detects_duplicates():
    example = QuestionExample(
        source_exam="sample",
        question_number=2,
        instruction="",
        stem="비가 그친 후에 공원에 ( ).",
        choices=["갔다", "간다", "가겠다", "가곤 한다"],
        answer=1,
    )
    question = GeneratedQuestion.model_validate(sample_question())
    issues = validate_question(question, [example])

    assert any(issue.code == "exact_duplicate" for issue in issues)


def test_payload_and_storage_round_trip(tmp_path):
    storage = Storage(tmp_path / "lab.db")
    payload = {"questions": [sample_question("주말에는 친구와 전시회를 ( ).")]}
    questions, issues = validate_generation_payload(payload, [])
    result = manual_result("gemini", "test-model", "generation", json.dumps(payload, ensure_ascii=False))
    run_id = storage.save_run(result, "system", "user")
    storage.add_generated_questions(
        run_id,
        "gemini",
        "test-model",
        [question.model_dump() for question in questions],
        [[issue.model_dump() for issue in group] for group in issues],
    )
    item = storage.list_generated()[0]
    storage.save_review(item["id"], Review(approved=True, naturalness=5))

    saved = storage.list_generated()[0]
    assert saved["question"]["answer"] == 1
    assert saved["review"]["approved"] is True


def test_rescan_fills_only_missing_example_metadata(tmp_path):
    storage = Storage(tmp_path / "lab.db")
    original = QuestionExample(
        source_exam="sample",
        question_number=1,
        instruction="",
        stem="문장 ( ).",
        choices=["가", "나", "다", "라"],
        grammar_point="사람이 입력한 문법",
    )
    storage.upsert_examples([original])
    rescanned = original.model_copy(
        update={
            "answer": 2,
            "grammar_point": "파서 문법",
            "rationale": "새로 발견한 정답 설명",
        }
    )

    storage.upsert_examples([rescanned])
    saved = storage.list_examples()[0]

    assert saved.answer == 2
    assert saved.grammar_point == "사람이 입력한 문법"
    assert saved.rationale == "새로 발견한 정답 설명"


def test_exports_only_approved_items():
    item = {
        "provider": "openai",
        "model": "test-model",
        "question": sample_question(),
        "validation": [],
        "review": Review(approved=True).model_dump(),
    }
    rejected = {**item, "review": Review(approved=False).model_dump()}

    assert "비가 그친" in to_txt([item, rejected])
    assert len(json.loads(to_json([item, rejected]))) == 1
    assert to_csv([item, rejected]).count("\n") == 2
