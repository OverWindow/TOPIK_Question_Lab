from copy import deepcopy
import json

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from test_vocabulary_questions import CONFIG, FakeBank, bank, bundle, finish, result, review
from topik_question_lab.common.vocabulary.vocabulary_models import source_snapshot, validate_bundle
from topik_question_lab.common.vocabulary.vocabulary_questions import question_status
from topik_question_lab.menus.vocabulary_generation.vocabulary_bulk import VocabularyRunner
from topik_question_lab.menus.vocabulary_generation.vocabulary_prompts import build_prompt, prompt_config, UNSPECIFIED_POS_PROMPT


EXPRESSION = source_snapshot({
    "vocabulary_id": 1, "korean": "그럼에도 불구하고", "part_of_speech": "", "english": "even so",
    "vietnamese": "mặc dù vậy", "japanese": "それにもかかわらず", "topik_level": 5,
    "example_korean": "비용이 든다. 그럼에도 불구하고 필요하다.",
    "example_english": "It costs money. Even so, it is necessary.",
})


def expression_bundle(*, relation=False):
    questions = [{
        "position": position, "question_type": "sentence_blank", "instruction": "빈칸에 알맞은 표현을 고르세요.",
        "stem": stem, "choices": ["그 결과", "예를 들어", "다시 말해", "그럼에도 불구하고"],
        "correct_answer": 4, "accepted_answers": ["그럼에도 불구하고"],
        "explanation": "앞의 상황에서 예상하는 것과 다른 일이 이어집니다.",
    } for position, stem in enumerate((
        "비가 거세게 내렸다. ( ) 선수들은 경기를 끝까지 치렀다.",
        "여행 경비가 크게 올랐다. ( ) 우리는 여행을 취소하지 않았다.",
        "여러 번 실패했다. ( ) 연구팀은 도전을 포기하지 않았다.",
    ), 1)]
    if relation:
        questions.append({
            "position": 7, "question_type": "synonym", "instruction": "밑줄 친 표현과 비슷한 뜻을 고르세요.",
            "stem": "비용이 든다. ‘그럼에도 불구하고’ 필요하다.",
            "choices": ["그런데도", "따라서", "예컨대", "즉"], "correct_answer": 1,
            "accepted_answers": [], "explanation": "앞의 상황에도 예상과 다른 판단이 이어집니다.",
        })
    return {"target_meaning": "앞의 상황에도 예상과 다른 일이 이어짐", "questions": questions,
            "skipped_reason": "" if relation else "문맥에서 의미 관계가 분명한 문항을 생략합니다."}


@pytest.mark.parametrize("pos", ["", " \t\n", None])
@pytest.mark.parametrize("relation", [False, True])
def test_unspecified_pos_accepts_usage_questions_but_rejects_conjugation(pos, relation):
    word = {**EXPRESSION, "part_of_speech": pos}
    value = validate_bundle(expression_bundle(relation=relation), word)
    assert [q.position for q in value.questions] == ([1, 2, 3, 7] if relation else [1, 2, 3])
    assert all(q.accepted_answers == [word["korean"]] for q in value.questions[:3])
    with pytest.raises(ValueError, match="필수 문항"):
        validate_bundle(bundle(), word)


@pytest.mark.parametrize("stage", ["generate", "review", "repair"])
def test_unspecified_pos_policy_reaches_every_stage_with_custom_prompts(stage):
    config = prompt_config({"system": "기존 공통 지침", "generation_prompt": "기존 생성 지침",
                            "review_prompt": "기존 검수 지침", "repair_prompt": "기존 수정 지침"})
    checkpoint = {"candidate": expression_bundle(), "issues": ["2번 문맥 보완"], "repair_positions": [2]}
    word = {key: value for key, value in EXPRESSION.items() if key != "part_of_speech"}
    prompt = build_prompt(stage, word, checkpoint, config)
    assert UNSPECIFIED_POS_PROMPT in prompt
    assert json.loads(prompt.split("단어 자료: ", 1)[1].split("\n", 1)[0]) == word
    assert config["system"] == "기존 공통 지침"
    for pos in ("동사", "형용사", "명사", "부사"):
        assert UNSPECIFIED_POS_PROMPT not in build_prompt(stage, {**word, "part_of_speech": pos}, checkpoint, config)


def expression_caller(calls, *, outcome="pass"):
    def invoke(**kwargs):
        calls.append(kwargs)
        assert UNSPECIFIED_POS_PROMPT in kwargs["user_prompt"]
        if len(calls) == 1:
            payload = expression_bundle(relation=outcome == "optional_rejected")
        elif kwargs["operation"] == "vocabulary_review":
            candidate = json.loads(kwargs["user_prompt"].split("검수할 묶음: ", 1)[1])
            failed = ()
            if outcome == "optional_rejected":
                failed = (7,)
            elif outcome == "repair_rejected" or outcome == "repair" and len(calls) == 2:
                failed = (2,)
            payload = review(candidate, failed=failed)
        else:
            candidate = json.loads(kwargs["user_prompt"].split("기존 묶음: ", 1)[1].split("\n수정 대상 position:", 1)[0])
            question = deepcopy(candidate["questions"][1])
            question["stem"] = "여행 경비가 크게 올랐다. ( ) 우리는 계획대로 여행을 떠났다."
            payload = {"questions": [question]}
        return result(payload, kwargs["operation"])
    return invoke


@pytest.mark.parametrize("outcome", ["pass", "repair", "optional_rejected", "repair_rejected"])
def test_expression_generation_review_repair_and_auto_save(tmp_path, outcome):
    fake, calls = FakeBank(), []
    fake.snapshots[1]["word"] = deepcopy(EXPRESSION)
    runner = VocabularyRunner(tmp_path, expression_caller(calls, outcome=outcome), lambda _: fake)
    batch_id = runner.start("fake", snapshots=[fake.snapshot(1)], config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    expected_calls = 4 if outcome in {"repair", "repair_rejected"} else 2
    assert len(calls) == expected_calls
    assert fake.snapshot(1)["word"] == EXPRESSION
    if outcome == "repair_rejected":
        assert not fake.commits and task["status"] == "pending"
        assert "2번: 복수 정답 가능" in task["error"]
        return
    assert task["status"] == "saved"
    saved = fake.snapshot(1)
    assert question_status(saved) == "ready"
    assert [q["position"] for q in saved["existing"]] == [1, 2, 3]
    assert all(q["accepted_answers"] == [EXPRESSION["korean"]] for q in saved["existing"])
    if outcome == "repair":
        before = json.loads(calls[1]["user_prompt"].split("검수할 묶음: ", 1)[1])["questions"]
        after = task["checkpoint"]["candidate"]["questions"]
        assert [after[0], after[2]] == [before[0], before[2]]
        assert after[1]["stem"] != before[1]["stem"]
    if outcome == "optional_rejected":
        assert task["checkpoint"]["review"]["optional_skipped"]


def test_new_expression_attempt_preserves_old_pos_rejection_history(tmp_path):
    fake, calls = FakeBank(), []
    fake.snapshots[1]["word"] = deepcopy(EXPRESSION)
    runner = VocabularyRunner(tmp_path, expression_caller(calls), lambda _: fake)
    old_batch = runner.store.create(fake.identity(), {**CONFIG, **prompt_config(), "prompt_version": "vocabulary-6"},
                                    [fake.snapshot(1)])
    task_id = runner.store.tasks(old_batch)[0]["generation_id"]
    runner.store.update(task_id, status="needs_attention", error="품사를 확인한 뒤 생성하세요.")
    old_task = runner.store.task(task_id)
    batch_id = runner.start("fake", snapshots=[fake.snapshot(1)], config=CONFIG)
    finish(runner)
    assert runner.store.tasks(batch_id)[0]["status"] == "saved" and len(calls) == 2
    assert runner.store.task(task_id) == old_task


@pytest.mark.parametrize("outcome", ["pass", "repair"])
def test_expression_postgres_save_and_completed_selection(bank, tmp_path, outcome):
    fields = [key for key in EXPRESSION if key not in {"vocabulary_id", "chinese_simplified", "indonesian"}]
    with psycopg.connect(bank.url) as c:
        c.execute(f"UPDATE topik_bank.vocabulary SET {','.join(key + '=%s' for key in fields)} WHERE vocabulary_id=1",
                  [EXPRESSION[key] for key in fields])
    original = bank.snapshot(1)["word"]
    assert bank.progress() == {"total": 1, "completed": 0, "remaining": 1}
    calls = []
    runner = VocabularyRunner(tmp_path, expression_caller(calls, outcome=outcome))
    batch_id = runner.start(bank.url, snapshots=bank.select(), config=CONFIG)
    finish(runner)
    task = runner.store.tasks(batch_id)[0]
    assert task["status"] == "saved"
    assert bank.confirm_saved(task, CONFIG, bank.identity())
    saved = bank.snapshot(1)
    assert saved["word"] == original and question_status(saved) == "ready"
    assert [q["question_type"] for q in saved["existing"]] == ["sentence_blank"] * 3
    assert bank.select() == [] and bank.progress() == {"total": 1, "completed": 1, "remaining": 0}


def test_expression_subjective_preview_preserves_whole_answer():
    def show(questions, word):
        from topik_question_lab.menus.vocabulary_generation.vocabulary_questions_ui import preview
        preview(questions, word, key="expression-preview")

    app = AppTest.from_function(show, args=(expression_bundle()["questions"], EXPRESSION["korean"])).run()
    assert not app.exception
    app.radio(key="expression-preview").set_value("주관식").run()
    assert not app.exception
    assert any("‘그럼에도 불구하고’를 사용해 빈칸을 채우세요." in item.value for item in app.markdown)
    assert sum("허용 답안: 그럼에도 불구하고" in item.value for item in app.markdown) == 3
