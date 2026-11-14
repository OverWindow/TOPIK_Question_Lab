import json
import threading
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.maintenance.backup_service import BackupOptions, create_backup, restore_backup
from topik_question_lab.common.generation.bulk_generation import BulkRunner, BulkStore, custom_model, model_options, prepare_batch, profiles_for, storage_for
from topik_question_lab.common.generation.generation_service import save_generation
from topik_question_lab.common.listening_models import ListeningQuestionExample
from topik_question_lab.common.models import ProviderResult, QuestionExample, Review
from topik_question_lab.common.generation.providers import GENERATION_PRESETS, generation_preset_prompt
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog
from topik_question_lab.common.generation.topic_bank import TopicBrief, TopicStore


def seed(root, section):
    for type_id, profile in profiles_for(section).items():
        storage = storage_for(root, section, type_id)
        for slot in profile.question_numbers:
            if section == "reading":
                example = QuestionExample(source_exam="fixture", question_number=slot, question_type=type_id,
                                          instruction="예시", stem=f"원본 기출 문장 {slot}",
                                          choices=["원본 가", "원본 나", "원본 다", "원본 라"], approved=True)
            else:
                example = ListeningQuestionExample(source_exam="fixture", question_number=slot,
                                                   question_type=type_id, approved=True)
            storage.save_example(example)
        storage.set_setting("difficulty", f"사용자 난이도 {type_id}")
        storage.set_setting("generation_count", "30")
        storage.set_setting("generation_presets", '{"gpt_5_6_luna":"format_repair"}')
        storage.set_setting("active_providers", '["claude"]')


def response(snapshot):
    questions = []
    shared = snapshot["type_id"].startswith("paired_")
    for slot in snapshot["slots"]:
        base = {"type_slot": slot, "question_type": snapshot["type_id"],
                "answer": 1, "explanation": "정답은 문맥의 근거에 맞고 다른 보기는 조건이 다릅니다.",
                "choices": ["새 보기 가", "새 보기 나", "새 보기 다", "새 보기 라"],
                "set_id": "set-1" if shared else ""}
        content = f"새로운 {snapshot['type_id']} 자료에서 시민들의 활동과 연구의 결과를 소개합니다."
        if not shared:
            content += f" {slot}번의 독립된 세부 상황과 설명입니다."
        if snapshot["section"] == "reading":
            material = content + " ( ) (가) (나) (다) (라) (①) (②) (③) (④)"
            base.update(stem=content + ("" if snapshot["type_id"] == "similar_expression" else " ( )"),
                        passage=material, highlight_text="시민들의 활동", auxiliary_text="주어진 문장",
                        question_prompt=f"{slot}번 질문",
                        target_grammar="독해", difficulty="사용자 난이도")
        else:
            profile = profiles_for("listening")[snapshot["type_id"]]
            base.update(dialogue_turns=[{"speaker": "여자", "text": content}],
                        question_prompt="들은 내용과 같은 것을 고르십시오.", target_skill="내용 파악",
                        repeat_count=profile.repeat_count, visual_kind=profile.visual_kind)
            if profile.visual_kind != "none":
                base["choices"] = []
                base["visual_options"] = [{"number": number, "description": f"장면 설명 {number}",
                                            "image_prompt": f"흑백 그림 프롬프트 {number}"} for number in range(1, 5)]
        questions.append(base)
    return {"questions": questions}


def prepared(root, section, monkeypatch, preset="precise_generation"):
    monkeypatch.setenv("CHATKHU_API_KEY", "fake-test-key")
    seed(root, section)
    snapshots, errors = prepare_batch(root, section, "gpt_5_6_luna", "gpt-5.6-luna", preset)
    assert not errors
    return snapshots


def caller_for(snapshots, calls):
    by_prompt = {item["user"]: item for item in snapshots}

    def fake(provider, model, operation, system, user, generation_preset=None):
        snapshot = by_prompt[user]
        calls.append((snapshot["type_id"], provider, model, generation_preset, system, user))
        payload = response(snapshot)
        return ProviderResult(provider=provider, model=model, operation=operation,
                              raw_response=json.dumps(payload), parsed_json=payload,
                              generation_preset=generation_preset)
    return fake


def wait(runner):
    runner.thread.join(15)
    assert not runner.thread.is_alive()


@pytest.mark.parametrize("section,requests", [("reading", 18), ("listening", 21)])
@pytest.mark.parametrize("preset", list(GENERATION_PRESETS))
def test_full_batch_fixes_one_model_and_preset_without_overwriting_settings(tmp_path, monkeypatch, section, requests, preset):
    snapshots = prepared(tmp_path, section, monkeypatch, preset)
    calls = []
    runner = BulkRunner(tmp_path, caller_for(snapshots, calls))
    batch_id = runner.start(snapshots)
    wait(runner)
    assert runner.store.batches()[0]["status"] == "succeeded"
    assert len(calls) == requests
    assert {(call[1], call[2], call[3]) for call in calls} == {("gpt_5_6_luna", "gpt-5.6-luna", preset)}
    assert all(call[4].count("[문제 생성 프리셋]") == 1 for call in calls)
    slots = []
    for snapshot in snapshots:
        storage = storage_for(tmp_path, section, snapshot["type_id"])
        items = storage.list_generated()
        slots.extend(item["question"]["type_slot"] for item in items)
        assert all(item["batch_id"] == batch_id and item["review"]["approved"] for item in items)
        assert storage.get_setting("difficulty") == f"사용자 난이도 {snapshot['type_id']}"
        assert storage.get_setting("generation_count") == "30"
        assert storage.get_setting("generation_presets") == '{"gpt_5_6_luna":"format_repair"}'
        assert storage.get_setting("active_providers") == '["claude"]'
    assert sorted(slots) == list(range(1, 51))
    assert len(collect_candidate_catalog(tmp_path).candidates) == 50


@pytest.mark.parametrize("kind", ["api", "json", "missing", "duplicate", "passage", "set_id"])
def test_failed_types_retry_with_original_snapshot_only(tmp_path, monkeypatch, kind):
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    target = next(item for item in snapshots if item["type_id"] == "paired_19_20")
    calls = []
    normal = caller_for(snapshots, calls)
    fail = True

    def fake(*args, **kwargs):
        result = normal(*args, **kwargs)
        if fail and args[4] == target["user"]:
            if kind in {"api", "json"}:
                return result.model_copy(update={"error": "timeout" if kind == "api" else "Invalid JSON",
                                                 "raw_response": "broken", "parsed_json": None})
            payload = result.parsed_json
            if kind == "missing": payload["questions"].pop()
            if kind == "duplicate": payload["questions"][1]["type_slot"] = 19
            if kind == "passage": payload["questions"][1]["passage"] = "다른 공통 지문"
            if kind == "set_id": payload["questions"][1]["set_id"] = "another"
            result = result.model_copy(update={"raw_response": json.dumps(payload), "parsed_json": payload})
        return result

    runner = BulkRunner(tmp_path, fake)
    batch_id = runner.start(snapshots)
    wait(runner)
    assert runner.store.batches()[0]["status"] == "partial"
    assert not any(r.candidate.review.get("approved") for r in collect_candidate_catalog(tmp_path).records)
    assert sum(task["count"] for task in runner.store.tasks(batch_id)) == 48
    storage = storage_for(tmp_path, "reading", target["type_id"])
    assert not storage.list_generated()
    assert storage.list_runs()[0]["result_json"]
    storage.set_setting("difficulty", "변경된 설정")
    fail = False
    runner.start(batch_id=batch_id)
    wait(runner)
    assert len(calls) == 19
    assert calls[-1][4:] == (target["system"], target["user"])
    assert runner.store.batches()[0]["status"] == "succeeded"
    assert len(storage.list_generated()) == 2


def test_resume_reconciles_commit_before_checkpoint_and_topic_history(tmp_path, monkeypatch):
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    store = BulkStore(tmp_path)
    batch_id = store.create(snapshots)
    first = snapshots[0]
    payload = response(first)
    result = ProviderResult(provider=first["provider"], model=first["model"], operation="generation", parsed_json=payload)
    storage = storage_for(tmp_path, "reading", first["type_id"])
    outcome = save_generation(storage, result, first["system"], first["user"], section="reading",
                              type_id=first["type_id"], expected_count=first["count"], batch_id=batch_id, strict=True)
    assert outcome.ok
    calls = []
    runner = BulkRunner(tmp_path, caller_for(snapshots, calls))
    assert store.batches()[0]["status"] == "interrupted"
    runner.start(batch_id=batch_id)
    wait(runner)
    assert len(calls) == 17
    assert len(storage.list_generated()) == 2
    assert store.batches()[0]["status"] == "succeeded"


def test_single_worker_rejects_duplicate_start_across_runner_instances(tmp_path, monkeypatch):
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    started, release = threading.Event(), threading.Event()
    calls = []
    normal = caller_for(snapshots, calls)

    def blocking(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return normal(*args, **kwargs)

    runner = BulkRunner(tmp_path, blocking)
    runner.start(snapshots)
    assert started.wait(5)
    second = BulkRunner(tmp_path, normal)
    try:
        with pytest.raises(RuntimeError): runner.start(snapshots)
        with pytest.raises(RuntimeError): second.start(snapshots)
        with pytest.raises(RuntimeError): create_backup(tmp_path)
        with pytest.raises(RuntimeError): restore_backup(tmp_path, b"unused")
        assert len(second.store.batches()) == 1
    finally:
        release.set()
        wait(runner)
    assert runner.store.batches()[0]["status"] == "succeeded"


def test_atomic_storage_failure_does_not_leave_half_saved_questions(tmp_path):
    storage = storage_for(tmp_path, "reading", "grammar_blank")
    with storage.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON generated_questions
                              WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    result = ProviderResult(provider="test", model="test", operation="generation")
    with pytest.raises(Exception, match="test failure"):
        storage.save_generation_result(result, "s", "u", "grammar_blank", "standard",
                                       [{"type_slot": 1}, {"type_slot": 2}], [[], []], "batch")
    assert not storage.list_generated()
    assert not storage.list_runs()
    assert storage.generation_receipt("batch") is None


def test_preflight_requires_api_and_examples_and_detects_pool_configuration(tmp_path, monkeypatch):
    seed(tmp_path, "reading")
    monkeypatch.delenv("CHATKHU_API_KEY", raising=False)
    storage = storage_for(tmp_path, "reading", "grammar_blank")
    with storage.connect() as connection: connection.execute("DELETE FROM examples")
    snapshots, errors = prepare_batch(tmp_path, "reading", "gpt_5_6_luna", "gpt-5.6-luna", "precise_generation")
    assert any("API 키" in error for error in errors)
    assert any("승인된 기출" in error for error in errors)
    with pytest.raises(ValueError): BulkStore(tmp_path).create(snapshots)


def test_unsupported_model_uses_default_api_settings(tmp_path, monkeypatch):
    seed(tmp_path, "reading")
    monkeypatch.setenv("CHATKHU_API_KEY", "fake")
    snapshots, errors = prepare_batch(tmp_path, "reading", "claude", "claude-haiku-4-5-20251001", "fast_draft")
    assert not errors
    assert all(item["preset"] is None and "[문제 생성 프리셋]" not in item["system"] for item in snapshots)


def test_media_catalog_is_excluded_and_custom_text_model_remains_selectable(tmp_path):
    storage = storage_for(tmp_path, "reading", "grammar_blank")
    storage.set_setting("chatkhu_models", json.dumps(["gpt-image-2", "gemini-2.5-pro-preview-tts", "veo-3", "tenant-chat-model"]))
    models = {model for _, model in model_options(tmp_path)}
    assert "tenant-chat-model" in models
    assert not models & {"gpt-image-2", "gemini-2.5-pro-preview-tts", "veo-3"}
    assert custom_model("deepseek-v4-pro")[0] == "deepseek_v4_pro"
    assert "deepseek-flash" in models
    assert custom_model("deepseek-flash") == ("deepseek_v4_1_flash", "deepseek-flash")
    with pytest.raises(ValueError): custom_model("gpt-image-2")


@pytest.mark.parametrize("preset", list(GENERATION_PRESETS))
def test_preset_instruction_is_replaced_once(preset):
    first = generation_preset_prompt("원래 지침", "fast_draft")
    final = generation_preset_prompt(first, preset)
    assert final.count("[문제 생성 프리셋]") == 1
    assert GENERATION_PRESETS[preset].label in final
    assert generation_preset_prompt(final, None) == "원래 지침"


def test_bulk_db_and_question_membership_round_trip_in_backup(tmp_path, monkeypatch):
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    runner = BulkRunner(tmp_path, caller_for(snapshots, []))
    batch_id = runner.start(snapshots)
    wait(runner)
    artifact = create_backup(tmp_path, BackupOptions(include_sources=False))
    assert next(item for item in artifact.manifest["files"] if item["path"] == "data/bulk_generation.db")["category"] == "generation_database"
    destination = tmp_path / "restored"
    restore_backup(destination, artifact.data)
    restored = BulkRunner(destination)
    assert restored.store.batches()[0]["id"] == batch_id
    assert restored.store.batches()[0]["status"] == "succeeded"
    assert sum(task["count"] for task in restored.store.tasks(batch_id)) == 50
    assert storage_for(destination, "reading", "grammar_blank").generation_receipt(batch_id)["count"] == 2


@pytest.mark.parametrize(("type_id", "slot", "source"), [
    ("sentence_insertion", 41, "global"),
    ("main_topic", 35, "slot_pool"),
])
def test_topic_usage_retry_is_idempotent(tmp_path, type_id, slot, source):
    store = TopicStore(tmp_path / "topics.db")
    brief = store.select_plan(section="reading", question_type=type_id, units=[("item-1", [slot])])[0]
    assert brief.source == source
    kwargs = dict(section="reading", question_type=type_id, provider="test", model="test", run_id=1, usage_key=f"batch:{type_id}")
    store.record_usages([brief], **kwargs)
    store.record_usages([brief], **kwargs)
    assert len(store.list_usages()) == (1 if source == "global" else 0)
    assert len(store.list_slot_pool_usages()) == (1 if source == "slot_pool" else 0)


def test_publication_collects_auto_approved_batch_and_respects_rejection(tmp_path, monkeypatch):
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    runner = BulkRunner(tmp_path, caller_for(snapshots, []))
    batch_id = runner.start(snapshots)
    wait(runner)
    catalog = collect_candidate_catalog(tmp_path)
    assert {record.candidate.batch_id for record in catalog.records} == {batch_id}
    assert len(catalog.candidates) == 50
    storage = storage_for(tmp_path, "reading", "grammar_blank")
    item = storage.list_generated()[0]
    storage.save_review(item["id"], Review(approved=False, notes="사람이 승인 취소"))
    runner._approve_batch(batch_id)
    candidates = collect_candidate_catalog(tmp_path).candidates
    assert len(candidates) == 49
    assert all(c.batch_id == batch_id for c in candidates)
    assert storage.list_generated()[0]["review"]["notes"] == "사람이 승인 취소"


def test_bulk_page_renders_and_disables_unprepared_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(tmp_path))
    monkeypatch.delenv("CHATKHU_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    root = Path(__file__).resolve().parents[1]
    app = AppTest.from_file(str(root / "topik_question_lab/menus/bulk_generation/bulk_generation_app.py"), default_timeout=30).run()
    assert not app.exception
    assert app.radio(key="bulk-section").value == "listening"
    assert not next(e for e in app.expander if e.label == "고급 설정").proto.expanded
    assert next(widget for widget in app.selectbox if widget.label == "생성 프리셋").value == "precise_generation"
    assert app.button(key="bulk-start-listening").disabled
    app.radio(key="bulk-section").set_value("reading").run()
    assert app.button(key="bulk-start-reading").disabled
    picker = next(widget for widget in app.selectbox if widget.label == "모델")
    picker.set_value(("claude", "claude-haiku-4-5-20251001")).run()
    assert not app.exception
    assert next(widget for widget in app.selectbox if widget.label == "생성 프리셋").disabled
    picker = next(widget for widget in app.selectbox if widget.label == "모델")
    picker.set_value(("deepseek_v4_1_flash", "deepseek-flash")).run()
    assert not app.exception
    preset = next(widget for widget in app.selectbox if widget.label == "생성 프리셋")
    assert not preset.disabled
    assert len(preset.options) == 4
    assert any("실제 모델 ID: deepseek-flash" in caption.value for caption in app.caption)
    preset.set_value("fast_draft").run()
    next(widget for widget in app.selectbox if widget.label == "생성 프리셋").set_value("diverse_draft").run()
    assert not app.exception
    assert next(widget for widget in app.selectbox if widget.label == "생성 프리셋").value == "diverse_draft"
    app.run()
    assert next(widget for widget in app.selectbox if widget.label == "모델").value == ("deepseek_v4_1_flash", "deepseek-flash")
    assert next(widget for widget in app.selectbox if widget.label == "생성 프리셋").value == "diverse_draft"


def test_bulk_page_runs_to_completion_and_remembers_common_preset(tmp_path, monkeypatch):
    import topik_question_lab.common.generation.bulk_generation as bulk

    snapshots = prepared(tmp_path, "reading", monkeypatch, "fast_draft")
    calls = []
    runner = BulkRunner(tmp_path, caller_for(snapshots, calls))
    runner.store.set_setting("presets", json.dumps({"gpt-5.6-luna": "fast_draft"}))
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(bulk, "BulkRunner", lambda root: runner)
    monkeypatch.setattr(bulk, "prepare_batch", lambda root, section, *args:
                        (snapshots, []) if section == "reading" else ([], ["듣기 준비 필요"]))
    root = Path(__file__).resolve().parents[1]
    app = AppTest.from_file(str(root / "topik_question_lab/menus/bulk_generation/bulk_generation_app.py"), default_timeout=30).run()
    assert not app.exception
    assert next(widget for widget in app.selectbox if widget.label == "생성 프리셋").value == "fast_draft"
    app.radio(key="bulk-section").set_value("reading").run()
    next(widget for widget in app.checkbox if widget.label == "음원·이미지 생성 및 DB 자동 등록").uncheck().run()
    app.button(key="bulk-start-reading").click().run()
    assert not app.exception
    wait(runner)
    app.run()
    assert not app.exception
    assert any("검수 완료(자동 승인)" in message.value for message in app.success)
    assert not app.button(key="bulk-start-reading").disabled
    assert not app.button(key="bulk-resume").disabled
    assert app.button(key="bulk-resume").label == "이어서 완료"
    assert len(calls) == 18


@pytest.mark.parametrize("exam_format,total", [("pbt", 50), ("ibt", 30)])
@pytest.mark.parametrize("section", ["reading", "listening"])
def test_one_click_starts_selected_exam_with_media_registration(monkeypatch, exam_format, total, section):
    from types import SimpleNamespace
    import topik_question_lab.common.generation.bulk_generation as bulk
    import topik_question_lab.common.generation.runner_service as runners
    import topik_question_lab.common.media.media_generation as media

    starts, preflights = [], []
    config = {"settings": {}, "target": "synthetic-target"}
    store = SimpleNamespace(settings=lambda: {}, set_setting=lambda *args: None, batches=lambda: [])
    runner = SimpleNamespace(store=store, thread=None, recover_idle=lambda: None,
                             start=lambda snapshots, **kwargs: starts.append((snapshots, kwargs)) or "new-batch")
    monkeypatch.setattr(runners, "get_runner", lambda _: runner)
    monkeypatch.setattr(bulk, "model_options", lambda _: [("deepseek_v4_1_flash", "deepseek-flash")])
    monkeypatch.setattr(bulk, "prepare_batch", lambda root, selected, provider, model, preset, exam:
                        ([{"slots": [1], "type_id": "synthetic", "count": 1, "approved_count": 1,
                           "difficulty": "standard", "section": selected, "exam_format": exam}], []))
    monkeypatch.setattr(media, "preflight", lambda root, settings, selected, url, exam:
                        preflights.append((selected, exam)) or config)
    page = Path(__file__).resolve().parents[1] / "topik_question_lab/menus/bulk_generation/bulk_generation_app.py"
    app = AppTest.from_file(str(page), default_timeout=30).run()
    app.radio(key="bulk-exam-format").set_value(exam_format)
    app.radio(key="bulk-section").set_value(section).run()
    button = app.button(key=f"bulk-start-{section}")
    assert f"{total}문제 생성하고 등록" in button.label
    button.click().run()
    assert not app.exception
    assert preflights == [(section, exam_format)] and len(starts) == 1
    assert starts[0][0][0]["section"] == section
    assert starts[0][0][0]["exam_format"] == exam_format
    assert starts[0][1] == {"media_config": config}


def test_generation_batch_review_filter_shows_only_selected_batch(tmp_path, monkeypatch):
    # A small UI harness exercises the same filter used by review and publication.
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(tmp_path))
    harness = tmp_path / "filter_app.py"
    harness.write_text('''import streamlit as st
from topik_question_lab.common.ui.batch_ui import select_generation_batch
items=[{"batch_id":None,"id":1},{"batch_id":"batch-a","id":2},{"batch_id":"batch-b","id":3}]
filtered=select_generation_batch(items,"filter",lambda value:value["batch_id"])
st.write([item["id"] for item in filtered])
''', encoding="utf-8")
    app = AppTest.from_file(str(harness)).run()
    app.selectbox[0].set_value("batch-a").run()
    assert not app.exception
    assert app.json[0].value == '[2]'
    app.selectbox[0].set_value("legacy").run()
    assert app.json[0].value == '[1]'
