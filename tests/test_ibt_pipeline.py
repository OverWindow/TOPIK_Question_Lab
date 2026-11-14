from topik_question_lab.common.ui.navigation import menu_page
import io
import json

from PIL import Image
import pytest

from test_bulk_generation import seed
from topik_question_lab.common.models import ProviderResult
from topik_question_lab.common.listening_models import ChartSpec
from topik_question_lab.common.media.listening_visuals import render_chart, render_banner
from topik_question_lab.common.generation.exam_profiles import ibt_profiles, profile_for
from topik_question_lab.common.generation.bulk_generation import prepare_batch, BulkRunner, storage_for
from topik_question_lab.common.media.media_generation import media_plan, chart_spec
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog, validate_complete_selection
from topik_question_lab.common.question_bank.direct_publication import make_package, package_draft
from topik_question_lab.common.generation.validation import validate_generation_payload
from topik_question_lab.common.generation.listening_validation import validate_listening_payload


COMBO = dict(chart_type="combo", title="도시 인구", labels=["2020년", "2025년"], values=[100, 120],
             unit="만명", series_name="인구", line_values=[10, 15], line_unit="%", line_name="고령 비율")
COMBO_TEXT = "도시 인구 인구: 2020년: 100만명, 2025년: 120만명. 고령 비율: 2020년: 10%, 2025년: 15%."


def payload(snapshot):
    profile = profile_for(snapshot["section"], snapshot["type_id"], snapshot["generation_profile"])
    user = snapshot["user"]
    result = json.loads(user[user.rfind('{\n  "questions":'):])
    for q in result["questions"]:
        slot = q["type_slot"]
        shared = profile.shared_passage or profile.shared_script
        content = f"새로운 {profile.type_id} 자료에 관한 구체적인 사실과 설명이다."
        if not shared:
            content += f" 독립된 상황 {slot}."
        if snapshot["section"] == "reading":
            if any("빈칸" in role for role in profile.roles):
                content += " 따라서 ( ) 필요하다."
            if profile.highlight_numbers:
                content += " 마음이 무거워졌다."
                q["highlight_text"] = "마음이 무거워졌다" if slot in profile.highlight_numbers else ""
            if profile.content_mode == "sentence_order":
                content += " (가) 처음 (나) 다음 (다) 이후 (라) 끝"
            if profile.content_mode == "sentence_insertion":
                content += " (①) 첫째 (②) 둘째 (③) 셋째 (④) 마지막"
            if profile.material_kind == "material_combo_chart":
                content = COMBO_TEXT
                q["visual_material"] = {"chart_spec": COMBO}
            elif profile.material_kind == "material_banner":
                q["visual_material"] = {"text": content}
            q.update(stem=content, passage="" if profile.base_type_id == "grammar_blank" else content)
        else:
            q["dialogue_turns"] = [{"speaker": "여자", "text": content}, {"speaker": "남자", "text": "그렇군요. 함께 확인해 봅시다."}]
        q.update(exam_format="ibt", generation_profile=snapshot["generation_profile"])
    return result


def generated_batch(root, section, monkeypatch, fail_type=None):
    monkeypatch.setenv("CHATKHU_API_KEY", "test-only")
    seed(root, section)
    snapshots, errors = prepare_batch(root, section, "gpt_5_6_luna", "gpt-5.6-luna", "precise_generation", "ibt")
    assert not errors
    by_prompt = {s["user"]: s for s in snapshots}
    calls = []

    def call(provider, model, operation, system, user, **kwargs):
        snapshot = by_prompt[user]
        calls.append(snapshot["type_id"])
        data = payload(snapshot)
        if snapshot["type_id"] == fail_type:
            data["questions"].pop()
        return ProviderResult(provider=provider, model=model, operation=operation, parsed_json=data)

    runner = BulkRunner(root, call)
    batch_id = runner.start(snapshots)
    runner.thread.join(30)
    assert not runner.thread.is_alive()
    return runner, batch_id, snapshots, calls


@pytest.mark.parametrize("section,types,images,audio", [("reading", 13, 4, 0), ("listening", 14, 8, 22)])
def test_ibt_generation_catalog_and_media(tmp_path, monkeypatch, section, types, images, audio):
    runner, bid, snapshots, calls = generated_batch(tmp_path, section, monkeypatch)
    batch = runner.store.batches()[0]
    assert batch["status"] == "succeeded", runner.store.tasks(bid)
    assert batch["exam_format"] == "ibt" and len(calls) == types
    catalog = collect_candidate_catalog(tmp_path)
    assert not catalog.read_errors
    assert len(catalog.candidates) == 30, catalog.exclusions
    assert sorted(c.slot for c in catalog.candidates) == list(range(1, 31))
    assert {c.exam_format for c in catalog.candidates} == {"ibt"}
    package = make_package(batch, catalog.records)
    plan = media_plan(package, None)
    assert sum(t["kind"] == "audio" for t in plan) == audio
    assert sum(t["kind"] == "image" for t in plan) == images
    for task in plan:
        if task["kind"] == "audio":
            repetitions = [s for s in task["script"]["segments"] if s["kind"] == "dialogue"]
            assert len(repetitions) == (2 if len(task["positions"]) == 2 else 1)
    for snapshot in snapshots:
        storage = storage_for(tmp_path, section, snapshot["type_id"])
        assert all(q["question"]["generation_profile"] for q in storage.list_generated())
        assert all(not e.question_type.startswith("ibt_") for e in storage.list_examples())


def test_ibt_roles_and_invalid_count_are_rejected(tmp_path, monkeypatch):
    runner, bid, snapshots, _ = generated_batch(tmp_path, "reading", monkeypatch, "ibt_purpose_content")
    assert runner.store.batches()[0]["status"] == "partial"
    snapshot = next(s for s in snapshots if s["type_id"] == "ibt_purpose_content")
    data = payload(snapshot)
    for q in data["questions"]:
        q["passage"] += " ( )"
    _, issues = validate_generation_payload(data, [], question_type=snapshot["type_id"])
    assert any(i.code == "unexpected_blank" for group in issues for i in group)
    monkeypatch.setenv("CHATKHU_API_KEY", "test-only")
    seed(tmp_path, "listening")
    listening, errors = prepare_batch(tmp_path, "listening", "gpt_5_6_luna", "gpt-5.6-luna", None, "ibt")
    assert not errors
    snapshot = next(s for s in listening if s["type_id"] == "ibt_topic_reason")
    data = payload(snapshot)
    from topik_question_lab.common.question_bank.bank_editor import checked_question
    from topik_question_lab.common.question_bank.direct_publication import PublicationBlocked
    question = data["questions"][1]
    row = dict(section="listening", item_type=snapshot["type_id"], type_slot=28, content_json=question)
    with pytest.raises(PublicationBlocked, match="2회"):
        checked_question(row, {**question, "repeat_count": 1})
    data["questions"][1].update(question_role="내용 일치", repeat_count=1)
    _, issues = validate_listening_payload(data, [], snapshot["type_id"], expected_count=2)
    assert {i.code for group in issues for i in group} >= {"repeat_count", "question_role"}
    snapshot = next(s for s in listening if s["type_id"] == "ibt_visual_chart")
    data = payload(snapshot)
    for option in data["questions"][0]["visual_options"]:
        option["chart_spec"]["chart_type"] = "bar"
    _, issues = validate_listening_payload(data, [], snapshot["type_id"], expected_count=1)
    assert any(i.code == "chart_spec" for group in issues for i in group)


def test_ibt_combo_chart_accepts_grouped_population_values():
    spec = {**COMBO, "title": "도시 인구 현황", "labels": ["2020년", "2022년", "2024년"],
            "values": [9600000, 9450000, 9300000], "unit": "명", "series_name": "인구 수",
            "line_values": [16, 18, 20], "line_name": "65세 이상 인구 비율", "line_unit": "%"}
    source = ("도시 인구 현황\n인구 수: 2020년: 9,600,000명\n인구 수: 2022년: 9,450,000명\n"
              "인구 수: 2024년: 9,300,000명\n65세 이상 인구 비율: 2020년: 16%\n"
              "65세 이상 인구 비율: 2022년: 18%\n65세 이상 인구 비율: 2024년: 20%")
    assert chart_spec(spec, source) == spec
    with pytest.raises(ValueError):
        chart_spec(spec, source.replace("2022년: 18%", "2022년: 20%"))


def test_combo_chart_error_names_all_missing_series_and_gives_usable_example():
    spec = {**COMBO, "series_name": "서울시 인구 수", "line_name": "65세 이상 인구 비율"}
    source = ("도시 인구\n인구 수(막대): 2020년: 100만명, 2025년: 120만명\n"
              "65세 이상 인구 비율(꺾은선): 2020년: 10%, 2025년: 15%")
    with pytest.raises(ValueError) as error:
        chart_spec(spec, source)
    message = str(error.value)
    assert "‘서울시 인구 수:’" in message and "‘65세 이상 인구 비율:’" in message
    assert "'(막대)'·'(꺾은선)'" in message
    example = message.split("작성 예: ", 1)[1]
    assert chart_spec(spec, spec["title"] + "\n" + example) == spec


def test_mapping_and_renderers():
    assert len(ibt_profiles("reading")) == 13
    assert len(ibt_profiles("listening")) == 14
    old_chart = dict(chart_type="bar", title="조사", labels=["항목"], values=[10.0], unit="%")
    assert ChartSpec.model_validate(old_chart).model_dump() == old_chart
    assert chart_spec(COMBO, COMBO_TEXT)["line_values"] == [10, 15]
    with pytest.raises(ValueError):
        chart_spec(COMBO, COMBO_TEXT.replace("15%", "18%"))
    with pytest.raises(ValueError):
        chart_spec({**COMBO, "title": "다른 제목"}, COMBO_TEXT)
    for data in [render_chart(ChartSpec(**COMBO)), render_banner("하루 한 시간 꾸준히!\n건강한 내일을 준비하세요.")]:
        image = Image.open(io.BytesIO(data))
        image.verify()


def test_retry_uses_saved_mapping_and_does_not_duplicate(tmp_path, monkeypatch):
    runner, bid, snapshots, _ = generated_batch(tmp_path, "reading", monkeypatch, "ibt_purpose_content")
    by_prompt = {s["user"]: s for s in snapshots}
    retried = []

    def call(provider, model, operation, system, user, **kwargs):
        snapshot = by_prompt[user]
        retried.append(snapshot["type_id"])
        return ProviderResult(provider=provider, model=model, operation=operation, parsed_json=payload(snapshot))

    def unavailable(*args, **kwargs):
        raise AssertionError("재시도는 변경된 매핑을 읽으면 안 됩니다.")

    monkeypatch.setattr("topik_question_lab.common.generation.exam_profiles.ibt_profiles", unavailable)
    runner.provider_call = call
    runner.start(batch_id=bid)
    runner.thread.join(30)
    assert not runner.thread.is_alive()
    assert runner.store.batches()[0]["status"] == "succeeded", runner.store.tasks(bid)
    assert retried == ["ibt_purpose_content"]
    catalog = collect_candidate_catalog(tmp_path)
    assert not catalog.read_errors and len(catalog.candidates) == 30


@pytest.mark.parametrize("section,filename,key", [("reading", "app.py", "reading"), ("listening", "listening_app.py", "listening")])
def test_ibt_pages(tmp_path, monkeypatch, section, filename, key):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    generated_batch(tmp_path, section, monkeypatch)
    monkeypatch.setenv("CHATKHU_API_KEY", "")
    app = AppTest.from_file(str(menu_page(filename)), default_timeout=30).run()
    app.sidebar.radio(key=f"{key}-exam-format").set_value("ibt").run()
    assert not app.exception
    for stage in ("3. 프롬프트 작업실", "4. 문제 생성", "5. 검수·비교", "6. 내보내기"):
        app.sidebar.radio(key=f"{key}-work-stage").set_value(stage).run()
        assert not app.exception
    if section == "reading":
        app.sidebar.radio(key="reading-work-stage").set_value("5. 검수·비교").run()
        for type_id in ("ibt_short_text_topic", "ibt_chart_content_match", "ibt_purpose_content"):
            app.sidebar.selectbox[0].set_value(type_id).run()
            assert not app.exception
            next(b for b in app.button if b.label == "문제 수정 저장").click().run()
            assert not app.exception
            saved = storage_for(tmp_path, section, type_id).list_generated()[0]["question"]
            assert saved["exam_format"] == "ibt" and saved["generation_profile"]["type_id"] == type_id


def test_database_formats_and_migration(tmp_path, monkeypatch, postgres_database_factory):
    import psycopg
    from pathlib import Path
    from dataclasses import replace
    from test_postgres_integration import _draft
    from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank
    from topik_question_lab.common.question_bank.publication import build_set_draft
    url = postgres_database_factory()
    schema = Path("topik_question_lab/maintenance/target_bank.sql").read_text(encoding="utf-8")
    with psycopg.connect(url) as c:
        c.execute(schema)
    bank = PostgresQuestionBank(url)
    runner, bid, _, _ = generated_batch(tmp_path, "reading", monkeypatch)
    package = make_package(runner.store.batches()[0], collect_candidate_catalog(tmp_path).records)
    ibt = package_draft(package)
    pbt = build_set_draft([replace(i.candidate, provider_key=ibt.provider_key, model_id=ibt.model_id, backend=ibt.backend)
                           for i in _draft().items], {}, 4, 0.0)
    old = bank.publish_set(pbt)
    new = bank.publish_set(ibt)
    assert bank.publish_set(ibt).set_id == new.set_id
    assert old.set_id != new.set_id and new.set_sequence == 1
    contents = bank.list_current_set_contents()
    assert sum(r["exam_format"] == "pbt" for r in contents) == 50
    assert sum(r["exam_format"] == "ibt" for r in contents) == 30
    with psycopg.connect(url) as c:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("UPDATE topik_bank.items SET exam_format='pbt' WHERE item_id=%s", (ibt.items[0].candidate.item_id,))
    mixed = [i.candidate for i in ibt.items]
    mixed[0] = pbt.items[0].candidate
    with pytest.raises(ValueError):
        validate_complete_selection(mixed)


def test_migration_preserves_existing_pbt_and_media(postgres_database_factory):
    import psycopg
    from psycopg.types.json import Jsonb
    from pathlib import Path
    from test_postgres_integration import _draft
    from topik_question_lab.common.question_bank.direct_publication import expected_item
    from topik_question_lab.common.question_bank.postgres_storage import _source_provenance
    url = postgres_database_factory()
    base = Path("topik_question_lab/maintenance")
    old_schema = (base / "target_bank.sql").read_text(encoding="utf-8").split("-- Existing records remain PBT;")[0]
    draft = _draft()
    item = expected_item(draft.items[0])
    item.pop("exam_format")
    candidate = draft.items[0].candidate
    item.update(item_id=candidate.item_id, source_key=candidate.source_key,
                source_provenance=_source_provenance(candidate, draft.items[0].metadata))
    with psycopg.connect(url) as c:
        c.execute(old_schema)
        c.execute("""INSERT INTO topik_bank.items (""" + ",".join(item) + ") VALUES (" + ",".join(["%s"] * len(item)) + ")",
                  [Jsonb(v) if isinstance(v, (dict, list)) else v for v in item.values()])
        c.execute("""INSERT INTO topik_bank.question_sets
            (set_id,section,generator_provider,generator_model,generator_version,set_sequence,default_target_level)
            VALUES ('20000000-0000-0000-0000-000000000001','reading','test','test','test',1,4)""")
        c.execute("UPDATE topik_bank.items SET set_id='20000000-0000-0000-0000-000000000001',position=1")
        c.execute("""INSERT INTO topik_bank.item_visual_assets
            (visual_asset_id,item_id,content_hash,option_number,storage_bucket,storage_path,storage_url,mime_type,byte_size)
            VALUES ('10000000-0000-0000-0000-000000000001',%s,%s,1,'synthetic','kept.png','https://example.invalid/kept.png','image/png',100)""",
                  (item["item_id"], item["content_hash"]))
        before = c.execute("SELECT to_jsonb(i) FROM topik_bank.items i").fetchone()[0]
        set_before = c.execute("SELECT to_jsonb(s) FROM topik_bank.question_sets s").fetchone()[0]
        media = c.execute("SELECT to_jsonb(a) FROM topik_bank.item_visual_assets a").fetchone()[0]
        migration = (base / "migrations/010_exam_format.sql").read_text(encoding="utf-8")
        c.execute(migration)
        c.execute(migration)
        after = c.execute("SELECT to_jsonb(i) FROM topik_bank.items i").fetchone()[0]
        assert after == {**before, "exam_format": "pbt"}
        assert c.execute("SELECT to_jsonb(s) FROM topik_bank.question_sets s").fetchone()[0] == {**set_before, "exam_format": "pbt"}
        assert c.execute("SELECT to_jsonb(a) FROM topik_bank.item_visual_assets a").fetchone()[0] == media


@pytest.mark.parametrize("section,images,audio", [("reading", 4, 0), ("listening", 8, 22)])
def test_ibt_media_pipeline_and_registration(tmp_path, monkeypatch, postgres_database_factory, section, images, audio):
    import shutil
    from test_media_generation import pipeline, FakeGoogle, FakeCloud
    from topik_question_lab.common.media.media_registration import MediaRegistration
    from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank
    from topik_question_lab.common.question_bank.storage_overview import load_overview
    if section == "listening" and (not shutil.which("ffmpeg") or not shutil.which("ffprobe")):
        pytest.skip("FFmpeg required")
    url = postgres_database_factory()
    bank = PostgresQuestionBank(url)
    bank.ensure_schema()
    runner, bid, _, _ = generated_batch(tmp_path, section, monkeypatch)
    google, cloud = FakeGoogle(), FakeCloud()
    reg = MediaRegistration(tmp_path, url)
    run = pipeline(tmp_path, bid, google=google, cloud=cloud, registration=reg)
    run.run()
    overview = load_overview(tmp_path, url)
    assert overview["bank"]["available"] and not overview["errors"]
    saved = overview["bank"]["sets"][0]
    assert saved["exam_format"] == "ibt" and saved["item_count"] == 30
    assert saved["coverage"]["image_files"] == images
    assert saved["coverage"]["audio_files"] == audio
    assert saved["coverage"]["audio_bound"] == (30 if audio else 0)
    before = (len(google.speeches), len(google.images))
    pipeline(tmp_path, bid, google=google, cloud=cloud, registration=reg).run()
    assert (len(google.speeches), len(google.images)) == before
    assert len(bank.list_current_set_contents()) == 30
