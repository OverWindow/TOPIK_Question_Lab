from copy import deepcopy
import io
import json
from pathlib import Path
import uuid

import pytest
from streamlit.testing.v1 import AppTest

from test_direct_publication import seed_batch, local_package
from test_bank_transfer import data_editor_widget_events
from topik_question_lab.common.question_bank.bank_editor import BankEditor, prepare_edit, item_metadata, media_ready, set_health, upload_media
from topik_question_lab.common.question_bank.direct_publication import DirectPublicationService, PublicationBlocked, package_draft
from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank, PublicationStateChanged


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "topik_question_lab/menus/bank_editor/bank_editor_app.py"


def select_table_questions(app, item_ids):
    table = next(frame for frame in app.dataframe if '문항 ID' in frame.value.columns)
    states = app._tree.get_widget_states()
    edit = next(state for state in states.widgets if state.id == table.proto.id)
    edit.string_value = json.dumps({'edited_rows': {
        str(index): {'선택': item_id in item_ids} for index, item_id in enumerate(table.value['문항 ID'])
    }, 'added_rows': [], 'deleted_rows': []})
    app._run(states)
    assert not app.exception and not app.error
    assert set(app.session_state['bank-editor-selected']) == set(item_ids)


@pytest.fixture
def bank(postgres_database_factory, tmp_path, monkeypatch):
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    batch_id = seed_batch(tmp_path)
    service = DirectPublicationService(tmp_path, url)
    draft = package_draft(local_package(service, batch_id))
    PostgresQuestionBank(url).publish_set(draft)
    monkeypatch.setenv("DATABASE_URL", url)
    return BankEditor(url)


def pick(editor, slot=1, section="reading"):
    with editor.connect(True) as c:
        item_id = c.execute("SELECT item_id FROM topik_bank.items WHERE section=%s AND type_slot=%s", (section, slot)).fetchone()[0]
    return editor.load(item_id)


def edited(snapshot, **values):
    return {**deepcopy(snapshot["item"]["content_json"]), **values}


def seed_listening(editor, root):
    batch_id = seed_batch(root, "listening")
    service = DirectPublicationService(root, editor.url)
    PostgresQuestionBank(editor.url).publish_set(package_draft(local_package(service, batch_id)))


def audio_binding(editor, positions):
    with editor.connect() as c:
        aid = uuid.uuid4()
        c.execute("""INSERT INTO topik_bank.tts_audio_assets(audio_asset_id,source_hash,model_name,female_voice,male_voice,
            storage_bucket,storage_path,storage_url,byte_size,narration_version,script_snapshot,file_sha256)
            VALUES (%s,%s,'test','female','male','audio','old.mp3','https://example.test/old.mp3',5,'exam_track_v4','{}',%s)""",
            (aid, uuid.uuid4().hex * 2, "a" * 64))
        c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
            SELECT item_id,content_hash,%s,%s FROM topik_bank.items WHERE section='listening' AND type_slot=ANY(%s)""", (aid, "b" * 64, positions))
    return str(aid)


def visual_binding(editor, slot=10):
    with editor.connect() as c:
        c.execute("""INSERT INTO topik_bank.item_visual_assets(visual_asset_id,item_id,content_hash,option_number,visual_role,
            storage_bucket,storage_path,storage_url,mime_type,byte_size,file_sha256)
            SELECT %s,item_id,content_hash,1,'material','images','old.png','https://example.test/old.png','image/png',5,%s
            FROM topik_bank.items WHERE section='reading' AND type_slot=%s""", (uuid.uuid4(), "c" * 64, slot))


def fake_uploaded(replacement):
    audio = replacement["kind"] == "audio"
    return {"kind": replacement["kind"], "storage_backend": "s3", "storage_bucket": "test-bucket",
            "storage_path": "new.mp3" if audio else "new.png", "storage_url": "https://example.test/new",
            "mime_type": "audio/mpeg" if audio else "image/png", "byte_size": 5, "file_sha256": "d" * 64,
            **({"duration_ms": 5000} if audio else {"visual_role": replacement["visual_role"], "option_number": replacement["option_number"]})}


def test_editor_entry_without_database_does_not_create_files(tmp_path):
    before = list(tmp_path.rglob("*"))
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception
    assert app.title[0].value == "PostgreSQL 문제 관리"
    assert list(tmp_path.rglob("*")) == before


@pytest.fixture
def selection_editor(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from test_unversioned_questions import candidate, draft_item
    from topik_question_lab.common.models import GeneratedQuestion
    from topik_question_lab.common.question_bank.direct_publication import expected_item
    import topik_question_lab.common.question_bank.bank_editor as editing
    import topik_question_lab.common.question_bank.storage_overview as overview

    rows, loads, saves = [], [], []
    for number, label in enumerate(['첫째', '둘째', '셋째']):
        question = GeneratedQuestion(type_slot=1, stem=f'{label} 학생은 도서관에 ( ) 공부한다.',
            choices=['가서', '가면', '가지만', '가도록'], answer=1,
            explanation=f'{label} 원래 해설', target_grammar='연결 어미').model_dump()
        c = replace(candidate(offset=number * 100), question_type='grammar_blank', question=question)
        rows.append(dict(expected_item(draft_item(c)), item_id=str(c.item_id), set_id=None,
                         position=1, source_provenance={}))

    def load(target, item_id):
        loads.append(item_id)
        row = next(r for r in rows if r['item_id'] == item_id)
        return deepcopy(dict(target=target, item=row, items=[row], set=None,
                             media=dict(item_visual_assets=[], item_audio_bindings=[], tts_audio_assets=[])))

    def save(plan):
        saves.append(deepcopy(plan))
        for row in rows:
            if row['item_id'] in plan['changes']:
                row.update(deepcopy(plan['changes'][row['item_id']]), review_status='pending_review')
        return {'message': '선택 문항 저장 완료'}

    monkeypatch.setenv('DATABASE_URL', 'synthetic-source')
    monkeypatch.setattr(editing, 'BankEditor', lambda target:
                        SimpleNamespace(target=target, load=lambda item_id: load(target, item_id), save=save))
    monkeypatch.setattr(overview, 'read_bank', lambda _: deepcopy(dict(available=True, media_available=True, items=rows, sets=[])))
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    assert not app.exception and not app.error
    return SimpleNamespace(app=app, rows=rows, ids=[r['item_id'] for r in rows], loads=loads, saves=saves)


def test_checkboxes_open_only_selected_questions_and_skip_unchecked(selection_editor):
    page, app = selection_editor, selection_editor.app
    first, middle, last = page.ids
    assert not page.loads and not app.text_area
    assert not any(s.label == '편집할 문제' for s in app.selectbox)
    select_table_questions(app, [first, last])
    assert page.loads == [first]
    assert app.button(key='bank-editor-previous').disabled
    app.button(key='bank-editor-next').click().run()
    assert not app.error and page.loads == [first, last]
    assert app.session_state['bank-editor-choice'] == last
    assert app.button(key='bank-editor-next').disabled
    assert any(c.value == '선택 2문항 · 현재 2/2' for c in app.caption)
    app.button(key='bank-editor-previous').click().run()
    assert app.session_state['bank-editor-choice'] == first
    select_table_questions(app, [middle])
    assert app.session_state['bank-editor-choice'] == middle
    assert not any(b.key in {'bank-editor-next', 'bank-editor-previous'} for b in app.button)
    select_table_questions(app, [])
    assert not app.text_area and not page.saves


def test_filter_drops_hidden_selection_and_clears_empty_editor(selection_editor):
    page, app = selection_editor, selection_editor.app
    select_table_questions(app, [page.ids[0], page.ids[2]])
    app.text_input(key='bank-editor-search').set_value('셋째').run()
    assert not app.exception and not app.error
    assert app.session_state['bank-editor-selected'] == [page.ids[2]]
    assert app.session_state['bank-editor-choice'] == page.ids[2]
    app.text_input(key='bank-editor-search').set_value('존재하지않는내용').run()
    assert app.session_state['bank-editor-selected'] == [] and not app.text_area
    app.text_input(key='bank-editor-search').set_value('').run()
    assert not app.text_area and not page.saves
    assert not app.dataframe[0].value['선택'].any()


def test_refresh_retains_item_ids_when_order_changes_and_prunes_removed_items(selection_editor):
    page, app = selection_editor, selection_editor.app
    first, _, last = page.ids
    select_table_questions(app, [first, last])
    page.rows.reverse()
    next(b for b in app.button if b.label == '문제 목록 새로고침').click().run()
    assert not app.exception and not app.error
    assert app.session_state['bank-editor-choice'] == first
    table = app.dataframe[0].value
    assert table.loc[table['선택'], '문항 ID'].tolist() == [last, first]
    page.rows[:] = [r for r in page.rows if r['item_id'] != first]
    next(b for b in app.button if b.label == '문제 목록 새로고침').click().run()
    assert app.session_state['bank-editor-selected'] == [last]
    assert app.session_state['bank-editor-choice'] == last and not page.saves


def test_checked_question_confirmation_save_and_next_preserve_selection(selection_editor):
    page, app = selection_editor, selection_editor.app
    first, middle, last = page.ids
    originals = deepcopy(page.rows)
    select_table_questions(app, [first, last])
    next(w for w in app.text_area if w.label == '해설').set_value('체크한 문제의 수정 해설')
    next(b for b in app.button if b.label == '변경 내용 확인').click().run()
    assert not app.exception and not app.error
    assert not page.saves and page.rows == originals
    next(b for b in app.button if b.label == '편집으로 돌아가기').click().run()
    assert next(w for w in app.text_area if w.label == '해설').value == '체크한 문제의 수정 해설'
    assert app.session_state['bank-editor-selected'] == [first, last]
    next(b for b in app.button if b.label == '변경 내용 확인').click().run()
    next(b for b in app.button if b.label == 'PostgreSQL에 저장').click().run()
    assert not app.exception and not app.error
    assert len(page.saves) == 1 and page.saves[0]['affected'] == [first]
    assert page.rows[1:] == originals[1:]
    assert page.rows[0]['explanation'] == '체크한 문제의 수정 해설'
    assert app.session_state['bank-editor-selected'] == [first, last]
    app.button(key='bank-editor-next').click().run()
    assert app.session_state['bank-editor-choice'] == last
    assert next(w for w in app.text_area if w.label == '해설').value == originals[2]['explanation']
    assert middle not in page.loads


def test_external_item_link_checks_exact_item_outside_previous_filters(selection_editor):
    page, app = selection_editor, selection_editor.app
    select_table_questions(app, [page.ids[0]])
    app.text_input(key='bank-editor-search').set_value('첫째').run()
    next(w for w in app.text_area if w.label == '해설').set_value('저장하지 않은 수정')
    next(b for b in app.button if b.label == '변경 내용 확인').click().run()
    assert not app.error and not page.saves
    app.session_state['bank-editor-open-item'] = page.ids[2]
    app.run()
    assert not app.exception and not app.error
    assert app.session_state['bank-editor-selected'] == [page.ids[2]]
    assert app.session_state['bank-editor-choice'] == page.ids[2]
    assert app.text_input(key='bank-editor-search').value == ''
    app.run()
    assert app.session_state['bank-editor-choice'] == page.ids[2] and not page.saves


def test_database_change_clears_checked_questions(selection_editor, monkeypatch):
    page, app = selection_editor, selection_editor.app
    select_table_questions(app, [page.ids[0]])
    before = list(page.loads)
    monkeypatch.setenv('DATABASE_URL', 'different-source')
    app.run()
    assert not app.exception and not app.error
    assert app.session_state['bank-editor-selected'] == []
    assert not app.text_area and page.loads == before and not page.saves


def test_direct_edit_without_local_original_preserves_identity_and_columns(bank, tmp_path):
    snapshot = pick(bank)
    # The editor never reads local generation records, even if they disappear.
    for path in tmp_path.glob("data/types/*.db"):
        path.unlink()
    q = edited(snapshot, stem="오늘은 도서관에 ( ) 공부하려고 한다.", choices=["가서", "가면", "가지만", "가도록"], answer=2, explanation="새 해설")
    meta = {**item_metadata(snapshot["item"]), "target_level": 5, "predicted_difficulty": 1.2, "primary_skill": "문법 판단"}
    bank.save(prepare_edit(snapshot, q, meta))
    current = bank.load(snapshot["item"]["item_id"])
    row = current["item"]
    for key in ("item_id", "source_key", "set_id", "position", "item_type", "type_slot", "generator_version", "created_at"):
        assert row[key] == snapshot["item"][key]
    assert row["choices"] == q["choices"] == row["content_json"]["choices"]
    assert row["stem"] == q["stem"] and row["correct_answer"] == 2 and row["explanation"] == "새 해설"
    assert row["stem_length"] == len(q["stem"].strip()) and row["choice_count"] == 4
    assert row["target_level"] == 5 and row["source_provenance"]["publication_metadata"]["predicted_difficulty"] == 1.2
    assert row["review_status"] == current["set"]["review_status"] == "pending_review"
    assert row["content_hash"] != snapshot["item"]["content_hash"] and row["source_provenance"]["bank_edited_at"]


@pytest.mark.parametrize("field,value", [("question_type", "main_topic"), ("type_slot", 2), ("set_id", "other")])
def test_fixed_fields_rejected(bank, field, value):
    snapshot = pick(bank)
    with pytest.raises(PublicationBlocked, match="변경"):
        prepare_edit(snapshot, edited(snapshot, **{field: value}), item_metadata(snapshot["item"]))
    assert bank.load(snapshot["item"]["item_id"]) == snapshot


def test_shared_passage_updates_only_matching_siblings_atomically(bank):
    snapshot = pick(bank, 19)
    old = {r["type_slot"]: r for r in snapshot["items"]}
    passage = "밑줄표현 새로운 공통 지문 ( )"
    plan = prepare_edit(snapshot, edited(snapshot, passage=passage), item_metadata(snapshot["item"]))
    assert len(plan["affected"]) == 2
    bank.save(plan)
    current = bank.load(snapshot["item"]["item_id"])
    new = {r["type_slot"]: r for r in current["items"]}
    for slot in (19, 20):
        assert new[slot]["content_json"]["passage"] == passage
        assert passage in new[slot]["stem"] and new[slot]["review_status"] == "pending_review"
    assert new[18] == old[18] and new[21] == old[21]


def test_changed_sibling_or_media_blocks_all_edits(bank):
    snapshot = pick(bank, 19)
    plan = prepare_edit(snapshot, edited(snapshot, explanation="저장되지 않을 변경"), item_metadata(snapshot["item"]))
    with bank.connect() as c:
        c.execute("UPDATE topik_bank.items SET explanation='다른 작업' WHERE type_slot=20")
    with pytest.raises(PublicationStateChanged):
        bank.save(plan)
    assert pick(bank, 19)["item"]["explanation"] == snapshot["item"]["explanation"]
    snapshot = pick(bank, 10)
    visual_binding(bank)
    with pytest.raises(PublicationStateChanged):
        bank.save(prepare_edit(snapshot, edited(snapshot, explanation="오래된 화면"), item_metadata(snapshot["item"])))


def test_listening_shared_audio_invalidation_and_replacement(bank, tmp_path):
    seed_listening(bank, tmp_path)
    old_aid = audio_binding(bank, [21, 22])
    snapshot = pick(bank, 21, "listening")
    q = edited(snapshot, dialogue_turns=[{"speaker": "여자", "text": "수정한 공통 대본입니다."}])
    bank.save(prepare_edit(snapshot, q, item_metadata(snapshot["item"])))
    current = pick(bank, 21, "listening")
    pair = [r for r in current["items"] if r["type_slot"] in (21, 22)]
    assert all(r["content_json"]["dialogue_turns"] == q["dialogue_turns"] for r in pair)
    assert not any(b["audio_asset_id"] == old_aid for b in current["media"]["item_audio_bindings"])
    assert not media_ready(current, current["item"])
    with pytest.raises(PublicationBlocked, match="미디어|음원"):
        bank.change_status(current, "approve")
    replacement = {"kind": "audio", "data": b"new audio"}
    plan = prepare_edit(current, current["item"]["content_json"], item_metadata(current["item"]), [replacement])
    bank.save(plan, uploader=fake_uploaded)
    latest = pick(bank, 21, "listening")
    assert media_ready(latest, latest["item"])
    assert len(latest["media"]["item_audio_bindings"]) == 2
    assert latest["media"]["tts_audio_assets"][0]["script_snapshot"]["positions"] == [21, 22]


def test_unrelated_text_edit_rebinds_unchanged_media(bank, tmp_path):
    seed_listening(bank, tmp_path)
    aid = audio_binding(bank, [4])
    snapshot = pick(bank, 4, "listening")
    bank.save(prepare_edit(snapshot, edited(snapshot, explanation="해설만 변경"), item_metadata(snapshot["item"])))
    current = pick(bank, 4, "listening")
    assert media_ready(current, current["item"])
    assert any(b["audio_asset_id"] == aid and b["content_hash"] == current["item"]["content_hash"] for b in current["media"]["item_audio_bindings"])
    visual_binding(bank)
    snapshot = pick(bank, 10)
    bank.save(prepare_edit(snapshot, edited(snapshot, explanation="이미지 유지"), item_metadata(snapshot["item"])))
    assert media_ready(pick(bank, 10), pick(bank, 10)["item"])
    current = pick(bank, 10)
    bank.save(prepare_edit(current, edited(current, passage="달라진 자료"), item_metadata(current["item"])))
    current = pick(bank, 10)
    assert not media_ready(current, current["item"])


def test_image_replacement_and_status_transitions(bank):
    visual_binding(bank)
    snapshot = pick(bank, 10)
    replacement = {"kind": "image", "visual_role": "material", "option_number": 1, "data": b"new image"}
    bank.save(prepare_edit(snapshot, edited(snapshot, passage="새로운 자료"), item_metadata(snapshot["item"]), [replacement]), uploader=fake_uploaded)
    current = pick(bank, 10)
    assert media_ready(current, current["item"])
    bank.change_status(current, "approve")
    current = pick(bank, 10)
    assert current["item"]["review_status"] == "reviewed"
    bank.change_status(current, "retire")
    current = pick(bank, 10)
    assert current["item"]["review_status"] == "retired" and not set_health(current["items"])["ready"]
    bank.change_status(current, "restore")
    assert pick(bank, 10)["item"]["review_status"] == "pending_review"


def test_delete_keeps_set_numbers_and_cleans_only_item_bindings(bank):
    visual_binding(bank)
    snapshot = pick(bank, 10)
    bank.change_status(snapshot, "delete")
    current = pick(bank, 11)
    assert set_health(current["items"])["label"] == "구성 미완성 · 사용 불가"
    assert len(current["items"]) == 49 and current["item"]["position"] == 11
    with bank.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.item_visual_assets").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 1


@pytest.mark.parametrize("composite", [False, True])
def test_delete_blocks_consumer_history_even_with_cascade(bank, composite):
    snapshot = pick(bank)
    with bank.connect() as c:
        if composite:
            c.execute("CREATE TABLE public.synthetic_usage(set_id uuid,position smallint,FOREIGN KEY(set_id,position) REFERENCES topik_bank.items(set_id,position) ON DELETE CASCADE)")
            c.execute("INSERT INTO public.synthetic_usage VALUES (%s,%s)", (snapshot["item"]["set_id"], snapshot["item"]["position"]))
        else:
            c.execute("CREATE TABLE public.synthetic_usage(item_id uuid REFERENCES topik_bank.items(item_id) ON DELETE CASCADE)")
            c.execute("INSERT INTO public.synthetic_usage VALUES (%s)", (snapshot["item"]["item_id"],))
    with pytest.raises(PublicationBlocked, match="사용 기록"):
        bank.change_status(snapshot, "delete")
    assert bank.load(snapshot["item"]["item_id"]) == snapshot
    with bank.connect(True) as c:
        assert c.execute("SELECT count(*) FROM public.synthetic_usage").fetchone()[0] == 1


def test_upload_failure_does_not_change_database(bank):
    snapshot = pick(bank, 10)
    plan = prepare_edit(snapshot, edited(snapshot, explanation="업로드 실패"), item_metadata(snapshot["item"]),
                        [{"kind": "image", "visual_role": "material", "option_number": 1, "data": b"image"}])
    def fail(_):
        raise PublicationBlocked("파일 검증 실패")
    with pytest.raises(PublicationBlocked):
        bank.save(plan, uploader=fail)
    assert bank.load(snapshot["item"]["item_id"]) == snapshot


def test_other_writer_during_upload_prevents_partial_save(bank):
    snapshot = pick(bank, 10)
    plan = prepare_edit(snapshot, edited(snapshot, explanation="경합 중 저장"), item_metadata(snapshot["item"]),
                        [{"kind": "image", "visual_role": "material", "option_number": 1, "data": b"image"}])
    def concurrent_upload(replacement):
        with bank.connect() as c:
            c.execute("UPDATE topik_bank.items SET explanation='다른 작업의 수정' WHERE item_id=%s", (snapshot["item"]["item_id"],))
        return fake_uploaded(replacement)
    with pytest.raises(PublicationStateChanged):
        bank.save(plan, uploader=concurrent_upload)
    current = pick(bank, 10)
    assert current["item"]["explanation"] == "다른 작업의 수정"
    assert not current["media"]["item_visual_assets"]


def test_image_choices_keep_unchanged_files_and_only_invalidate_changed_picture(bank, tmp_path):
    seed_listening(bank, tmp_path)
    with bank.connect() as c:
        for number in range(1, 5):
            c.execute("""INSERT INTO topik_bank.item_visual_assets(visual_asset_id,item_id,content_hash,option_number,
                storage_bucket,storage_path,storage_url,mime_type,byte_size,file_sha256)
                SELECT %s,item_id,content_hash,%s,'images',%s,'https://example.test/picture.png','image/png',5,%s
                FROM topik_bank.items WHERE section='listening' AND type_slot=1""", (uuid.uuid4(), number, f"{number}.png", "e" * 64))
    snapshot = pick(bank, 1, "listening")
    bank.save(prepare_edit(snapshot, edited(snapshot, explanation="그림 유지"), item_metadata(snapshot["item"])))
    current = pick(bank, 1, "listening")
    assert len(current["media"]["item_visual_assets"]) == 4
    q = edited(current)
    q["visual_options"][1]["description"] = "완전히 다른 장면"
    bank.save(prepare_edit(current, q, item_metadata(current["item"])))
    latest = pick(bank, 1, "listening")
    assert {v["option_number"] for v in latest["media"]["item_visual_assets"]} == {1, 3, 4}
    assert latest["item"]["choices"][1] == "완전히 다른 장면"


def test_repeat_count_edit_and_replacement_follow_current_shared_script(bank, tmp_path):
    seed_listening(bank, tmp_path)
    audio_binding(bank, [21, 22])
    snapshot = pick(bank, 21, "listening")
    replacement = {"kind": "audio", "data": b"single-reading audio"}
    bank.save(prepare_edit(snapshot, edited(snapshot, repeat_count=1), item_metadata(snapshot["item"]), [replacement]), uploader=fake_uploaded)
    current = pick(bank, 21, "listening")
    assert all(r["content_json"]["repeat_count"] == 1 for r in current["items"] if r["type_slot"] in (21, 22))
    script = current["media"]["tts_audio_assets"][0]["script_snapshot"]
    assert len([s for s in script["segments"] if s["kind"] == "dialogue"]) == 1


def test_representative_editors_render_without_mutations(bank, tmp_path):
    seed_listening(bank, tmp_path)
    with bank.connect(True) as c:
        before = c.execute("SELECT to_jsonb(i) FROM topik_bank.items i ORDER BY item_id").fetchall()
    app = AppTest.from_file(str(PAGE), default_timeout=25).run()
    for section, slot in (("reading", 10), ("reading", 19), ("listening", 1), ("listening", 21)):
        row = pick(bank, slot, section)["item"]
        select_table_questions(app, [row["item_id"]])
        assert not app.exception and not app.error
        if section == "listening":
            assert any(w.label.startswith("듣기 대본") for w in app.text_area)
        if slot == 1:
            assert len([w for w in app.text_area if "그림 설명" in w.label]) == 4
    with bank.connect(True) as c:
        assert c.execute("SELECT to_jsonb(i) FROM topik_bank.items i ORDER BY item_id").fetchall() == before


def test_ui_edit_confirmation_save_and_delete_confirmation(bank):
    app = AppTest.from_file(str(PAGE), default_timeout=25).run()
    assert not app.exception
    snapshot = pick(bank)
    select_table_questions(app, [snapshot["item"]["item_id"]])
    next(w for w in app.text_area if w.label == "해설").set_value("화면에서 편집한 해설")
    next(b for b in app.button if b.label == "변경 내용 확인").click().run()
    assert not app.exception
    assert pick(bank)["item"]["explanation"] != "화면에서 편집한 해설"
    next(b for b in app.button if b.label == "PostgreSQL에 저장").click().run()
    assert not app.exception
    assert pick(bank)["item"]["explanation"] == "화면에서 편집한 해설"
    next(w for w in app.selectbox if w.label == "문항 작업").set_value("delete").run()
    next(b for b in app.button if b.label == "작업 내용 확인").click().run()
    assert next(b for b in app.button if b.label == "영구 삭제 실행").disabled
    next(w for w in app.checkbox if "영구 삭제하며" in w.label).check().run()
    assert not next(b for b in app.button if b.label == "영구 삭제 실행").disabled


def test_registration_requires_acknowledgement_before_overwriting_bank_edit(bank, tmp_path):
    from topik_question_lab.menus.registration.registration_workflow import RegistrationWorkflow
    snapshot = pick(bank)
    bank.save(prepare_edit(snapshot, edited(snapshot, explanation="PostgreSQL 편집본"), item_metadata(snapshot["item"])))
    workflow = RegistrationWorkflow(tmp_path, bank.url)
    plan = workflow.prepare("update", [snapshot["item"]["source_key"]], {"level": 4, "difficulty": 0.0, "items": {}})
    app = AppTest.from_file(str(ROOT / "topik_question_lab/menus/registration/postgres_publish_app.py"), default_timeout=25).run()
    app.session_state["registration-plan"] = plan
    app.run()
    assert not app.exception
    assert next(b for b in app.button if b.label == "선택한 1문제 변경 내용 반영하기").disabled
    next(w for w in app.checkbox if "편집본을 SQLite" in w.label).check().run()
    assert not next(b for b in app.button if b.label == "선택한 1문제 변경 내용 반영하기").disabled
    assert pick(bank)["item"]["explanation"] == "PostgreSQL 편집본"


def test_upload_image_decodes_and_verifies_cloud_bytes(monkeypatch):
    from PIL import Image
    from topik_question_lab.common.media import media_cloud
    checked = []
    class Store:
        bucket = "test-bucket"
        base_url = "https://example.test"
        def __init__(self, *args):
            pass
        def upload(self, path, key, size, digest, mime):
            with Image.open(path) as image:
                assert image.size == (4, 4) and image.format == "PNG"
            assert key == f"media/replacements/{digest}.png"
            checked.append("upload")
        def verify(self, row, *, local_path):
            assert row["storage_backend"] == "s3" and local_path.exists()
            checked.append("verify")
        def close(self):
            checked.append("close")
    monkeypatch.setattr(media_cloud, "S3MediaStore", Store)
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(stream, format="JPEG")
    row = upload_media({"kind": "image", "visual_role": "material", "option_number": 1, "data": stream.getvalue()})
    assert row["mime_type"] == "image/png" and checked == ["upload", "verify", "close"]
    assert row["storage_bucket"] == "test-bucket"
    assert row["storage_path"] == f"media/replacements/{row['file_sha256']}.png"
    assert row["storage_url"] == f"https://example.test/{row['storage_path']}"
    with pytest.raises(PublicationBlocked, match="이미지"):
        upload_media({"kind": "image", "data": b"not an image"})


def test_generated_image_replaces_only_selected_choice(bank, tmp_path):
    from test_media_generation import FakeGoogle, settings
    from topik_question_lab.menus.bank_editor.bank_media_generation import generate_replacements
    seed_listening(bank, tmp_path)
    with bank.connect() as c:
        for n in range(1, 5):
            c.execute("""INSERT INTO topik_bank.item_visual_assets(visual_asset_id,item_id,content_hash,option_number,
                storage_bucket,storage_path,storage_url,mime_type,byte_size,file_sha256)
                SELECT %s,item_id,content_hash,%s,'images',%s,'https://example.test/old.png','image/png',5,%s
                FROM topik_bank.items WHERE section='listening' AND type_slot=1""", (uuid.uuid4(), n, f"{n}.png", "e" * 64))
    snapshot = pick(bank, 1, "listening")
    pending = prepare_edit(snapshot, snapshot["item"]["content_json"], item_metadata(snapshot["item"]),
                           [dict(kind="image", visual_role="choice", option_number=2, generate=True)], allow_generation=True)
    ready = generate_replacements(pending, settings=settings(image_delay=0), client=FakeGoogle())
    assert bank.load(snapshot["item"]["item_id"]) == snapshot
    bank.save(ready, uploader=fake_uploaded)
    current = pick(bank, 1, "listening")
    images = {a["option_number"]: a["storage_path"] for a in current["media"]["item_visual_assets"]}
    assert images == {1: "1.png", 2: "new.png", 3: "3.png", 4: "4.png"}
    assert current["item"]["review_status"] == "pending_review"


def test_generated_shared_audio_keeps_provenance_and_manual_upload_still_works(bank, tmp_path, monkeypatch):
    from test_media_generation import FakeGoogle, settings
    from topik_question_lab.menus.bank_editor import bank_media_generation as generation
    from topik_question_lab.common.question_bank.bank_editor import AUDIO_GENERATION_FIELDS
    seed_listening(bank, tmp_path)
    other_audio = audio_binding(bank, [4])
    audio_binding(bank, [21, 22])
    snapshot = pick(bank, 21, "listening")
    monkeypatch.setattr(generation, "compose", lambda script, speech, path, config: path.write_bytes(b"generated mp3"))
    monkeypatch.setattr(generation.MediaSettings, "validate", lambda *args, **kwargs: None)
    pending = prepare_edit(snapshot, edited(snapshot, dialogue_turns=[dict(speaker="여자", text="새로운 공통 대본입니다.")]),
                           item_metadata(snapshot["item"]), [dict(kind="audio", generate=True)], allow_generation=True)
    ready = generation.generate_replacements(pending, settings=settings(), client=FakeGoogle())
    assert bank.load(snapshot["item"]["item_id"]) == snapshot
    bank.save(ready, uploader=lambda r: {**fake_uploaded(r), **{k: r["generation"][k] for k in AUDIO_GENERATION_FIELDS}})
    current = pick(bank, 21, "listening")
    bindings = [b for b in current["media"]["item_audio_bindings"] if b["item_id"] in ready["affected"]]
    assert len(bindings) == 2 and len({b["audio_asset_id"] for b in bindings}) == 1
    asset = next(a for a in current["media"]["tts_audio_assets"] if a["audio_asset_id"] == bindings[0]["audio_asset_id"])
    assert asset["provider"] == "google_cloud_tts" and asset["model_name"] == settings().tts_model
    assert asset["female_voice"] == settings().female and asset["script_snapshot"]["positions"] == [21, 22]
    assert any(b["audio_asset_id"] == other_audio for b in current["media"]["item_audio_bindings"])
    manual = prepare_edit(current, current["item"]["content_json"], item_metadata(current["item"]), [dict(kind="audio", data=b"manual mp3")])
    bank.save(manual, uploader=fake_uploaded)
    current = pick(bank, 21, "listening")
    binding = next(b for b in current["media"]["item_audio_bindings"] if b["item_id"] == current["item"]["item_id"])
    asset = next(a for a in current["media"]["tts_audio_assets"] if a["audio_asset_id"] == binding["audio_asset_id"])
    assert asset["provider"] == asset["model_name"] == "manual_upload"
