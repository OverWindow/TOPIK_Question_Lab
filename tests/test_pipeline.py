import json

from topik_question_lab.exports import to_csv, to_json, to_txt
from topik_question_lab.highlights import highlighted_html, parse_highlight_marker
from topik_question_lab.models import EnrichmentPayload, GeneratedQuestion, QuestionExample, Review
from topik_question_lab.navigation import next_sequence_item
from topik_question_lab.prompt_profiles import (
    DEFAULT_PROVIDER_INSTRUCTIONS,
    QUESTION_TYPE_PROFILES,
    apply_provider_instruction,
)
from topik_question_lab.prompts import build_enrichment_prompt, build_generation_prompt
from topik_question_lab.providers import (
    DEFAULT_ACTIVE_PROVIDERS,
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


def test_review_navigation_moves_forward_without_wrapping():
    assert next_sequence_item(["q1", "q2", "q3"], "q1") == "q2"
    assert next_sequence_item(["q1", "q2", "q3"], "q3") is None
    assert next_sequence_item(["q1", "q2"], "missing") is None


def test_extract_json_accepts_code_fence_and_surrounding_text():
    assert extract_json('```json\n{"questions": []}\n```') == {"questions": []}
    assert extract_json('응답입니다. {"analysis": {"notes": "ok"}} 끝') == {"analysis": {"notes": "ok"}}


def test_manual_invalid_json_preserves_raw_response():
    result = manual_result("openai", "test-model", "generation", "not-json")

    assert result.raw_response == "not-json"
    assert result.parsed_json is None
    assert result.error


def test_storage_can_delete_database_and_sidecar_files(tmp_path):
    path = tmp_path / "sample.db"
    storage = Storage(path)
    sidecar = tmp_path / "sample.db-journal"
    sidecar.touch()

    deleted = storage.delete_files()

    assert path in deleted
    assert sidecar in deleted
    assert not path.exists()
    assert not sidecar.exists()


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


def test_question_type_and_provider_prompt_profiles():
    assert len(QUESTION_TYPE_PROFILES) == 17
    assert QUESTION_TYPE_PROFILES["grammar_blank"].implemented is True
    assert set(DEFAULT_PROVIDER_INSTRUCTIONS) == set(DEFAULT_PROVIDERS)

    common = apply_provider_instruction("system", "user", "claude", optimized=False)
    claude = apply_provider_instruction("system", "user", "claude", optimized=True)
    gemini = apply_provider_instruction("system", "user", "gemini", optimized=True)

    assert common == ("system", "user")
    assert claude[0] != gemini[0]
    assert claude[1] == gemini[1] == "user"


def test_generation_prompt_includes_question_type_rules():
    prompt = build_generation_prompt([], "분석서", 2, "초급")

    assert "1~2번 문법 빈칸" in prompt
    assert "형태가 비슷하지만 의미 기능이 다른" in prompt


def test_similar_expression_prompt_and_validation():
    prompt = build_generation_prompt([], "분석서", 2, "초급", "similar_expression")
    question = GeneratedQuestion(
        question_type="similar_expression",
        type_slot=3,
        stem="아침에 늦게 일어나는 바람에 기차를 놓쳤다.",
        highlight_text="일어나는 바람에",
        choices=["일어난 탓에", "일어난 김에", "일어나는 대신", "일어나는 대로"],
        answer=1,
        explanation="원인과 부정적 결과를 나타낸다.",
        target_grammar="-는 바람에",
    )

    assert '"question_type": "similar_expression"' in prompt
    assert '"highlight_text"' in prompt
    assert validate_question(question, []) == []


def test_manual_highlight_marker_and_preview():
    stem, highlight, error = parse_highlight_marker(
        "아침에 늦게 [[일어나는 바람에]] 기차를 놓쳤다.",
        "",
    )

    assert error == ""
    assert highlight == "일어나는 바람에"
    assert "[[" not in stem
    assert "<u>일어나는 바람에</u>" in highlighted_html(stem, highlight)


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
        "gpt_5_6_luna",
        "gpt_5_3_chat",
        "claude",
        "gemini_3_5_flash",
        "gemini",
        "k_exaone",
        "solar_pro3",
        "llama",
        "gemma",
        "gpt_5_4_nano",
    ]
    assert "deepseek" not in DEFAULT_PROVIDERS
    assert DEFAULT_ACTIVE_PROVIDERS == ["gpt_5_6_luna", "claude", "gemini_3_5_flash"]
    assert DEFAULT_PROVIDERS["gpt_5_6_luna"].model == "gpt-5.6-luna"
    assert DEFAULT_PROVIDERS["gpt_5_3_chat"].model == "gpt-5.3-chat-latest"
    assert DEFAULT_PROVIDERS["gemini_3_5_flash"].model == "gemini-3.5-flash"
    assert DEFAULT_PROVIDERS["gemini"].model == "gemini-3.1-flash-lite"
    assert DEFAULT_PROVIDERS["k_exaone"].model == "LGAI-EXAONE/K-EXAONE-236B-A23B"
    assert DEFAULT_PROVIDERS["solar_pro3"].model == "solar-pro3"
    assert DEFAULT_PROVIDERS["llama"].model == "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8"
    assert DEFAULT_PROVIDERS["gemma"].model == "google/gemma-3-27b-it"
    assert DEFAULT_PROVIDERS["gpt_5_4_nano"].model == "gpt-5.4-nano"
    assert can_gateway_call("k_exaone") is True
    assert can_gateway_call("tenant-specific-model") is True
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


def test_shared_passage_payload_requires_complete_consistent_sets():
    common = {
        "question_type": "paired_19_20",
        "stem": "공통 지문\n질문",
        "passage": "공통 지문",
        "set_id": "set-1",
        "choices": ["가", "나", "다", "라"],
        "answer": 1,
        "explanation": "해설",
        "target_grammar": "독해",
    }
    payload = {
        "questions": [
            {**common, "type_slot": 19, "question_prompt": "빈칸에 들어갈 말은?", "passage": "공통 ( ) 지문"},
            {**common, "type_slot": 20, "question_prompt": "글의 주제는?", "passage": "서로 다른 지문"},
        ]
    }

    _, issues = validate_generation_payload(payload, [], question_type="paired_19_20")

    assert any(issue.code == "set_structure" for group in issues for issue in group)


def test_storage_syncs_source_and_generated_shared_passages(tmp_path):
    storage = Storage(tmp_path / "paired.db")
    first = QuestionExample(
        source_exam="sample",
        question_number=19,
        instruction="",
        stem="옛 지문\n질문 19",
        passage="옛 지문",
        question_prompt="질문 19",
        set_key="sample:19-20",
        question_type="paired_19_20",
        choices=["가", "나", "다", "라"],
    )
    second = first.model_copy(update={"question_number": 20, "stem": "옛 지문\n질문 20", "question_prompt": "질문 20"})
    storage.upsert_examples([first, second])
    storage.save_example(first.model_copy(update={"passage": "새 지문", "stem": "새 지문\n질문 19"}))

    saved = {example.question_number: example for example in storage.list_examples()}
    assert saved[20].passage == "새 지문"
    assert saved[20].stem == "새 지문\n질문 20"


def test_run_and_prompt_version_store_type_and_mode(tmp_path):
    storage = Storage(tmp_path / "lab.db")
    result = manual_result("gemini", "test-model", "analysis", '{"analysis": {}}')
    storage.save_run(result, "system", "user", "grammar_blank", "optimized")
    storage.save_prompt_version("system", "guide", 2, "초급", "grammar_blank", "optimized")

    run = storage.list_runs()[0]
    version = storage.list_prompt_versions()[0]

    assert run["question_type"] == "grammar_blank"
    assert run["prompt_mode"] == "optimized"
    assert version["question_type"] == "grammar_blank"
    assert version["prompt_mode"] == "optimized"


def test_type_databases_keep_settings_isolated(tmp_path):
    grammar_storage = Storage(tmp_path / "grammar_blank.db")
    similar_storage = Storage(tmp_path / "similar_expression.db")

    grammar_storage.set_setting("analysis_guide", "문법 빈칸 분석")
    similar_storage.set_setting("analysis_guide", "유사 표현 분석")

    assert grammar_storage.get_setting("analysis_guide") == "문법 빈칸 분석"
    assert similar_storage.get_setting("analysis_guide") == "유사 표현 분석"


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


def test_structured_exports_include_passage_fields():
    question = sample_question()
    question.update(
        {
            "question_type": "sentence_insertion",
            "type_slot": 39,
            "stem": "주어진 문장\n지문\n질문",
            "passage": "지문 (①) (②) (③) (④)",
            "question_prompt": "어디에 들어가는가?",
            "auxiliary_text": "주어진 문장",
            "set_id": "",
        }
    )
    item = {
        "provider": "gemini",
        "model": "test-model",
        "question": question,
        "validation": [],
        "review": Review(approved=True).model_dump(),
    }

    assert "주어진 문장: 주어진 문장" in to_txt([item])
    csv_text = to_csv([item])
    assert "question_prompt" in csv_text
    assert "지문 (①) (②) (③) (④)" in csv_text
