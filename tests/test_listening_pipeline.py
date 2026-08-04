import json
import io
from pathlib import Path
from types import SimpleNamespace

from reportlab.pdfgen import canvas
from streamlit.testing.v1 import AppTest

from topik_question_lab.listening_comparison_pdf import _draw_visuals
from topik_question_lab.listening_importer import (
    apply_enrichment_payload,
    load_imported_examples,
    needs_transcription,
    placeholder_examples,
    ready_for_enrichment,
    save_edited_imported_example,
    save_imported_examples,
    validate_import_coverage,
)
from topik_question_lab.listening_exports import to_csv as listening_to_csv, to_txt as listening_to_txt
from topik_question_lab.listening_models import DialogueTurn, GeneratedListeningQuestion, VisualOption
from topik_question_lab.listening_profiles import LISTENING_TYPE_PROFILES
from topik_question_lab.listening_prompts import build_listening_enrichment_prompt
from topik_question_lab.listening_storage import ListeningStorage
from topik_question_lab.listening_validation import validate_listening_payload, validate_listening_question
from topik_question_lab.listening_visuals import render_chart, suggest_visual_prompt
from topik_question_lab.listening_models import ChartSpec
from topik_question_lab.navigation import next_sequence_item
from topik_question_lab.providers import call_provider


ROOT = Path(__file__).resolve().parents[1]


def sample_manifest(tmp_path: Path) -> dict:
    return {
        "exam": "sample",
        "source_pdf": str(tmp_path / "sample.pdf"),
        "sha256": "abc",
        "page_count": 26,
        "pages": [str(tmp_path / f"page-{number:02d}.png") for number in range(1, 27)],
    }


def test_listening_profiles_cover_all_slots_once_and_repeat_ranges():
    slots = [slot for profile in LISTENING_TYPE_PROFILES.values() for slot in profile.question_numbers]
    assert len(LISTENING_TYPE_PROFILES) == 21
    assert sorted(slots) == list(range(1, 51))
    assert len(slots) == len(set(slots))
    assert all(profile.repeat_count == (1 if max(profile.question_numbers) <= 20 else 2) for profile in LISTENING_TYPE_PROFILES.values())


def test_listening_source_review_navigation_moves_forward_without_wrapping():
    assert next_sequence_item(["exam-1", "exam-2", "exam-3"], "exam-1") == "exam-2"
    assert next_sequence_item(["exam-1", "exam-2", "exam-3"], "exam-3") is None


def test_comparison_pdf_visual_reader_accepts_model_like_objects():
    pdf = canvas.Canvas(io.BytesIO())
    source = SimpleNamespace(visual_kind="scene", source_page_image="", visual_options=[])

    assert _draw_visuals(pdf, 10, 100, 200, source) == 100


def test_placeholder_import_creates_50_reviewable_slots(tmp_path):
    examples = placeholder_examples(sample_manifest(tmp_path))
    assert len(examples) == 50
    assert examples[0].source_page == 1
    assert examples[20].source_page == 12
    assert examples[-1].source_page == 26
    assert examples[20].set_key == examples[21].set_key
    assert examples[0].parse_warning == ""
    assert needs_transcription(examples[0])
    assert validate_import_coverage(examples) == []


def test_legacy_transcription_status_is_not_shown_as_structure_note(tmp_path):
    example = placeholder_examples(sample_manifest(tmp_path))[0]
    legacy = {**example.model_dump(mode="json"), "parse_warning": "전사 필요"}

    assert type(example).model_validate(legacy).parse_warning == ""


def test_source_dialogue_edit_is_saved_to_recognition_json(tmp_path):
    import_root = tmp_path / "imports"
    exam_dir = import_root / "sample"
    examples = placeholder_examples(sample_manifest(tmp_path))
    save_imported_examples(exam_dir, examples)
    edited = examples[3].model_copy(
        update={"dialogue_turns": [DialogueTurn(speaker="여자", text="수정한 문항별 대본입니다.")]}
    )

    assert save_edited_imported_example(import_root, edited)
    saved = next(value for value in load_imported_examples(import_root) if value.source_key == edited.source_key)
    assert saved.dialogue_turns == edited.dialogue_turns


def test_source_dialogue_edit_survives_next_and_return_in_ui(tmp_path, monkeypatch):
    import_root = tmp_path / "data" / "listening" / "imports"
    exam_dir = import_root / "sample"
    examples = []
    for number in (1, 2):
        examples.append(
            placeholder_examples(sample_manifest(tmp_path))[number - 1].model_copy(
                update={
                    "dialogue_turns": [DialogueTurn(speaker="여자", text=f"최초 전사 대본 {number}")],
                    "question_prompt": "들은 내용과 같은 그림을 고르십시오.",
                    # Keep the visual options incomplete to prove that, like
                    # the reading editor, an unapproved partial item can still
                    # save a dialogue-only correction.
                    "visual_options": [],
                }
            )
        )
    save_imported_examples(exam_dir, examples)
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(tmp_path))

    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "listening_app.py"), default_timeout=20).run()
    assert not app.exception
    dialogue = next(field for field in app.text_area if field.label.startswith("대본 ·"))
    dialogue.set_value("여자: 사용자가 직접 수정한 대본")
    next(button for button in app.button if button.label == "저장하고 다음 문항").click().run()
    assert not app.exception

    selector = next(field for field in app.selectbox if field.label == "검토 문항")
    assert selector.value == examples[1].source_key
    selector.set_value(examples[0].source_key).run()
    assert not app.exception
    dialogue = next(field for field in app.text_area if field.label.startswith("대본 ·"))
    assert dialogue.value == "여자: 사용자가 직접 수정한 대본"

    storage = ListeningStorage(tmp_path / "data" / "listening" / "types" / "visual_scene.db")
    saved = next(value for value in storage.list_examples() if value.source_key == examples[0].source_key)
    assert saved.script_text == "여자: 사용자가 직접 수정한 대본"


def test_listening_validation_and_shared_script_rules():
    question = GeneratedListeningQuestion(
        question_type="next_response",
        type_slot=4,
        dialogue_turns=[DialogueTurn(speaker="여자", text="오늘 같이 점심 먹을까요?")],
        question_prompt="이어질 말로 가장 알맞은 것을 고르십시오.",
        choices=["네, 좋아요.", "어제 먹었어요.", "식당이 멀어요.", "점심은 음식이에요."],
        answer=1,
        explanation="제안에 대한 수락이다.",
        target_skill="이어질 말",
        repeat_count=1,
    )
    assert validate_listening_question(question, []) == []

    common = {
        "question_type": "paired_21_22",
        "dialogue_turns": [{"speaker": "여자", "text": "새 광고를 준비해요."}],
        "choices": ["가", "나", "다", "라"],
        "answer": 1,
        "explanation": "해설",
        "target_skill": "듣기",
        "repeat_count": 2,
        "set_id": "set-1",
    }
    payload = {"questions": [{**common, "type_slot": 21, "question_prompt": "중심 생각은?"}]}
    _, issues = validate_listening_payload(payload, [], "paired_21_22")
    assert any(issue.code == "set_slots" for group in issues for issue in group)


def test_visual_questions_can_pass_without_assets_or_chart_specs():
    base = {
        "dialogue_turns": [DialogueTurn(speaker="여자", text="이번 조사 결과를 보세요.")],
        "question_prompt": "들은 내용과 같은 것을 고르십시오.",
        "choices": [],
        "answer": 1,
        "explanation": "대본의 수치와 일치한다.",
        "target_skill": "시각 정보 일치",
        "repeat_count": 1,
    }
    scene = GeneratedListeningQuestion(
        **base,
        question_type="visual_scene",
        type_slot=1,
        visual_kind="scene",
        visual_options=[
            VisualOption(number=index, description=f"장면 {index}", image_prompt=f"흑백 장면 {index}")
            for index in range(1, 5)
        ],
    )
    chart = GeneratedListeningQuestion(
        **base,
        question_type="visual_chart",
        type_slot=3,
        visual_kind="chart",
        visual_options=[
            VisualOption(number=index, description=f"그래프 {index}", image_prompt=f"흑백 그래프 {index}")
            for index in range(1, 5)
        ],
    )

    assert not [issue for issue in validate_listening_question(scene, []) if issue.severity == "error"]
    assert not [issue for issue in validate_listening_question(chart, []) if issue.severity == "error"]


def test_visual_option_discards_incomplete_optional_chart_spec():
    option = VisualOption.model_validate(
        {
            "number": 1,
            "description": "그래프 선택지",
            "image_prompt": "흑백 막대그래프",
            "chart_spec": {"chart_type": "bar", "labels": ["A", "B"], "values": None},
        }
    )

    assert option.chart_spec is None


def test_visual_prompt_is_suggested_and_included_in_text_exports():
    option = VisualOption(number=1, description="남자가 우산을 쓰고 걷는 장면")
    suggested = suggest_visual_prompt(option, "scene")
    question = GeneratedListeningQuestion(
        question_type="visual_scene",
        type_slot=1,
        dialogue_turns=[DialogueTurn(speaker="여자", text="비가 오네요.")],
        question_prompt="대화와 같은 그림을 고르십시오.",
        answer=1,
        explanation="우산을 쓰는 장면이다.",
        target_skill="그림 일치",
        visual_kind="scene",
        visual_options=[
            VisualOption(number=index, description=f"장면 {index}", image_prompt=suggested)
            for index in range(1, 5)
        ],
    )
    items = [{"provider": "chatkhu", "model": "model", "question": question.model_dump(mode="json"), "review": {"approved": True}}]

    assert "남자가 우산을 쓰고 걷는 장면" in suggested
    assert "그림·그래프 프롬프트 1" in listening_to_txt(items)
    assert "visual_prompts" in listening_to_csv(items)


def test_listening_storage_keeps_data_separate(tmp_path):
    storage = ListeningStorage(tmp_path / "data" / "listening" / "types" / "next_response.db")
    example = placeholder_examples(sample_manifest(tmp_path))[3].model_copy(
        update={
            "dialogue_turns": [DialogueTurn(speaker="남자", text="안녕하세요?")],
            "question_prompt": "이어질 말은?",
            "choices": ["가", "나", "다", "라"],
            "answer": 1,
            "answer_source": "manual",
            "approved": True,
        }
    )
    storage.upsert_examples([example])
    saved = storage.list_examples()[0]
    assert saved.answer_source == "manual"
    assert saved.approved is True
    assert "listening" in str(storage.path)


def test_batch_enrichment_prompt_and_partial_response(tmp_path):
    base = placeholder_examples(sample_manifest(tmp_path))[3]
    first = base.model_copy(
        update={
            "dialogue_turns": [DialogueTurn(speaker="여자", text="오늘 같이 점심 먹을까요?")],
            "question_prompt": "이어질 말로 알맞은 것을 고르십시오.",
            "choices": ["좋아요.", "어제 갔어요.", "식당이에요.", "비가 와요."],
        }
    )
    second = first.model_copy(update={"source_exam": "sample-2", "question_number": 5})
    assert ready_for_enrichment(first)
    prompt = build_listening_enrichment_prompt([first, second], "next_response")
    assert first.source_key in prompt
    assert second.source_key in prompt
    assert "입력된 모든 source_key" in prompt

    updated, missing = apply_enrichment_payload(
        [first, second],
        {
            "enrichments": [
                {
                    "source_key": first.source_key,
                    "answer": 1,
                    "target_skill": "제안에 대한 응답",
                    "rationale": "제안을 자연스럽게 수락한다.",
                    "confidence": 0.93,
                }
            ]
        },
        "deepseek-v4-flash",
    )
    assert updated[0].answer == 1
    assert updated[0].answer_source == "ai_suggested"
    assert updated[0].enrichment_model == "deepseek-v4-flash"
    assert updated[0].approved is False
    assert missing == [second.source_key]


def test_chart_renderer_outputs_png():
    data = render_chart(ChartSpec(chart_type="bar", title="조사", labels=["A", "B"], values=[60, 40], unit="%"))
    assert data.startswith(b"\x89PNG")


def test_multimodal_provider_sends_image_content(monkeypatch, tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fake-png")
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(content='{"questions": []}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2))

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setenv("CHATKHU_API_KEY", "test")
    monkeypatch.setattr("openai.OpenAI", FakeClient)
    result = call_provider("gpt_5_6_luna", "test-model", "transcription", "system", "user", [image])
    assert not result.error
    content = captured["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_listening_app_all_stages_render():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "listening_app.py"), default_timeout=20).run()
    assert not app.exception
    assert app.sidebar.selectbox[0].label == "듣기 문제 유형"
    for stage in ["2. 유형 분석", "3. 프롬프트 작업실", "4. 문제 생성", "5. 검수·비교", "6. 내보내기"]:
        app.sidebar.radio[0].set_value(stage).run()
        assert not app.exception, stage


def test_main_app_exposes_reading_and_listening_navigation():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "main.py"), default_timeout=20).run()
    assert not app.exception
    source = (ROOT / "topik_question_lab" / "main.py").read_text(encoding="utf-8")
    assert 'title="TOPIK II 읽기"' in source
    assert 'title="TOPIK II 듣기"' in source
    assert 'title="백업·복원"' in source
