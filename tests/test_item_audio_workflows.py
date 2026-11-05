import json
from dataclasses import replace

import psycopg
import pytest

from test_bank_editor import bank, seed_listening, audio_binding, pick, edited, fake_uploaded
from test_ibt_pipeline import generated_batch
from test_media_generation import pipeline
from topik_question_lab.step_06_publication.bank_editor import prepare_edit, item_metadata, media_ready
from topik_question_lab.step_06_publication.media_registration import MediaRegistration, check_media
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog, build_item_drafts


@pytest.mark.parametrize("slots", [[4], [21, 22]])
def test_standalone_edit_replace_and_shared_invalidation_preserve_history(bank, tmp_path, slots):
    seed_listening(bank, tmp_path)
    old_audio = audio_binding(bank, slots)
    with bank.connect() as c:
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE section='listening' AND type_slot=ANY(%s)", (slots,))
        sets_before = c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0]
    snapshot = pick(bank, slots[0], "listening")
    assert snapshot["set"] is None and len(snapshot["items"]) == len(slots)
    assert media_ready(snapshot, snapshot["item"])
    bank.save(prepare_edit(snapshot, edited(snapshot, explanation="해설만 수정"), item_metadata(snapshot["item"])))
    snapshot = pick(bank, slots[0], "listening")
    assert media_ready(snapshot, snapshot["item"])
    assert {r["audio_asset_id"] for r in snapshot["media"]["item_audio_bindings"]} == {old_audio}
    replacement = [{"kind": "audio", "data": b"new audio"}]
    for _ in range(2):
        bank.save(prepare_edit(snapshot, snapshot["item"]["content_json"], item_metadata(snapshot["item"]), replacement), uploader=fake_uploaded)
        snapshot = pick(bank, slots[0], "listening")
    with bank.connect(True) as c:
        ids = [r["item_id"] for r in snapshot["items"]]
        assert c.execute("SELECT count(*) FROM topik_bank.item_audio_bindings WHERE item_id=ANY(%s::uuid[])", (ids,)).fetchone()[0] == len(slots)*2 + 1
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == sets_before
    bank.save(prepare_edit(snapshot, edited(snapshot, dialogue_turns=[{"speaker": "여자", "text": "변경한 공유 대본입니다."}]), item_metadata(snapshot["item"])))
    final = pick(bank, slots[0], "listening")
    assert not final["media"]["item_audio_bindings"]
    with bank.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.item_audio_bindings WHERE item_id=ANY(%s::uuid[])", (ids,)).fetchone()[0] == len(slots)*2 + 1


@pytest.mark.parametrize("section", ["reading", "listening"])
def test_ibt_registration_uses_only_item_bindings_and_retry_keeps_ids(postgres_database_factory, tmp_path, monkeypatch, section):
    import topik_question_lab.step_03_generation.media_generation as generation
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    _, bid, _, _ = generated_batch(tmp_path, section, monkeypatch)
    def compose(script, speech, path, config):
        path.write_bytes(b"synthetic-" + json.dumps(script, ensure_ascii=False).encode())
        return 1000
    monkeypatch.setattr(generation, "compose", compose)
    monkeypatch.setattr(generation, "validate_mp3", lambda *args: 1000)
    reg = MediaRegistration(tmp_path, url)
    pipeline(tmp_path, bid, registration=reg).run()
    job = reg.jobs.list()[0]
    assert job["status"] == "succeeded"
    package = job["payload"]["packages"][0]
    with psycopg.connect(url) as c:
        check_media(c, package)
        before = c.execute("SELECT to_jsonb(b) FROM topik_bank.item_audio_bindings b ORDER BY binding_id").fetchall()
        assert len(before) == (30 if section == "listening" else 0)
        assert c.execute("SELECT count(*) FROM topik_bank.tts_audio_assets").fetchone()[0] == (22 if section == "listening" else 0)
        assert c.execute("SELECT to_regclass('topik_bank.question_set_item_audio_bindings')").fetchone() == (None,)
    reg.register(package)
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_jsonb(b) FROM topik_bank.item_audio_bindings b ORDER BY binding_id").fetchall() == before


def test_standalone_sync_preserves_explanation_audio_and_invalidates_only_its_group(bank, tmp_path):
    seed_listening(bank, tmp_path)
    audio_binding(bank, [21, 22, 23])
    with bank.connect() as c:
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE section='listening' AND type_slot IN (21,22,23)")
    value = next(r.candidate for r in collect_candidate_catalog(tmp_path).records if r.candidate.section == "listening" and r.candidate.slot == 21)
    updated = replace(value, question={**value.question, "explanation": "새 해설", "exam_format": "pbt"})
    storage = PostgresQuestionBank(bank.url)
    storage.publish_items(build_item_drafts([updated], {}, 4, 0.0))
    assert media_ready(pick(bank, 21, "listening"), pick(bank, 21, "listening")["item"])
    changed = replace(updated, question={**updated.question, "dialogue_turns": [{"speaker": "여자", "text": "달라진 대본"}]})
    storage.publish_items(build_item_drafts([changed], {}, 4, 0.0))
    with bank.connect(True) as c:
        active_slots = [r[0] for r in c.execute("""SELECT i.type_slot FROM topik_bank.item_audio_bindings b
            JOIN topik_bank.items i USING (item_id) WHERE b.is_current""")]
        assert active_slots == [23]
        assert c.execute("SELECT count(*) FROM topik_bank.item_audio_bindings").fetchone()[0] == 4


def test_standalone_number_change_invalidates_numbered_audio(bank, tmp_path):
    seed_listening(bank, tmp_path)
    audio_binding(bank, [4])
    with bank.connect() as c:
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE section='listening' AND type_slot=4")
    value = next(r.candidate for r in collect_candidate_catalog(tmp_path).records if r.candidate.section == "listening" and r.candidate.slot == 4)
    moved = replace(value, question={**value.question, "type_slot": 5})
    PostgresQuestionBank(bank.url).publish_items(build_item_drafts([moved], {}, 4, 0.0))
    with bank.connect(True) as c:
        assert c.execute("SELECT count(*),count(*) FILTER(WHERE is_current) FROM topik_bank.item_audio_bindings").fetchone() == (1, 0)
