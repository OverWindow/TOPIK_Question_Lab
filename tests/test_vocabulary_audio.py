from dataclasses import replace
import io
import json
import math
from pathlib import Path
import struct
import wave

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from test_vocabulary import bank, PAGE
from test_vocabulary_transfer import banks, stored, send, prepare
from topik_question_lab.step_03_generation.media_provider import MediaSettings
from topik_question_lab.step_06_publication import vocabulary_audio as audio, vocabulary_transfer as transfer
from topik_question_lab.step_06_publication.vocabulary import search_vocabulary
from topik_question_lab.ui import vocabulary_audio_ui as ui


@pytest.fixture
def audio_bank(bank, tmp_path):
    audio.prepare_audio_schema(bank, tmp_path)
    return bank


@pytest.fixture
def settings():
    return MediaSettings(project="test-project", bucket="test-bucket", base_url="https://media.example.com",
                         tts_delay=0, retries=0)


@pytest.fixture
def media():
    class Speech:
        calls = []
        frequency = 440

        def speech(self, speaker, text, *, prompt=None):
            assert prompt == audio.WORD_NARRATION_PROMPT
            self.calls.append((speaker, text))
            data = io.BytesIO()
            with wave.open(data, "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                output.writeframes(b"".join(struct.pack("<h", round(4000 * math.sin(2 * math.pi * self.frequency * i / 24000)))
                                           for i in range(4800)))
            return data.getvalue()

    class Cloud:
        bucket = "test-bucket"
        objects = {}
        fail = False
        on_verify = None

        def upload(self, path, key, size, sha, mime):
            assert audio.digest_file(path) == (size, sha)
            assert mime == "audio/mpeg"
            self.objects[key] = path.read_bytes()

        def verify(self, row, *, local_path):
            assert self.objects[row["storage_path"]] == local_path.read_bytes()
            assert row["storage_url"].startswith("https://media.example.com/media/vocabulary-audio/")
            if self.on_verify:
                self.on_verify()
            if self.fail:
                raise RuntimeError("synthetic-secret")

    return Speech(), Cloud()


def generate(url, root, settings, media, ids=(1,), **kwargs):
    return audio.generate_vocabulary_audio(url, root, ids, settings=settings, client=media[0], cloud=media[1], **kwargs)


def word(url, key=1):
    return next(row for row in stored(url) if row["vocabulary_id"] == key)


def test_schema_upgrade_preserves_words_and_backup_can_restore(bank, tmp_path, postgres_database_factory):
    before = stored(bank)
    path = audio.prepare_audio_schema(bank, tmp_path)
    backup = json.loads(path.read_text(encoding="utf-8"))
    assert backup["rows"] == before
    assert all(row[key] is None for row in stored(bank) for key in audio.AUDIO_FIELDS)
    assert [{k: v for k, v in row.items() if k not in audio.AUDIO_FIELDS} for row in stored(bank)] == before
    assert audio.prepare_audio_schema(bank, tmp_path) is None
    restored = postgres_database_factory()
    with psycopg.connect(restored) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute(backup["create_table_sql"])
        transfer._upsert(c, backup["rows"])
    assert stored(restored) == before


def test_backup_failure_does_not_add_columns(bank, tmp_path, monkeypatch):
    before = stored(bank)
    monkeypatch.setattr(transfer, "_backup", lambda *a: (_ for _ in ()).throw(OSError("synthetic-secret")))
    with pytest.raises(audio.VocabularyAudioError) as error:
        audio.prepare_audio_schema(bank, tmp_path)
    assert "synthetic-secret" not in str(error.value)
    assert stored(bank) == before


def test_generate_real_mp3_skip_and_refresh_on_voice_change(audio_bank, tmp_path, settings, media):
    before = word(audio_bank)
    result = generate(audio_bank, tmp_path, settings, media)[0]
    assert result["status"] == "generated", result
    saved = word(audio_bank)
    assert saved["audio_duration_ms"] > 0
    assert saved["audio_generation_config"]["style_prompt"] == audio.WORD_NARRATION_PROMPT
    assert saved["audio_generation_config"]["version"] == "vocabulary_word_v2"
    assert saved["audio_storage_path"].endswith(saved["audio_file_sha256"] + ".mp3")
    assert {k: v for k, v in saved.items() if k not in audio.AUDIO_FIELDS} == {
        k: v for k, v in before.items() if k not in audio.AUDIO_FIELDS}
    assert audio.audio_status(saved, audio.audio_profile(settings)) == "ready"
    assert generate(audio_bank, tmp_path, settings, media)[0]["status"] == "skipped"
    assert len(media[0].calls) == 1
    assert audio.audio_status(saved, audio.audio_profile(settings, "남자")) == "stale"
    assert generate(audio_bank, tmp_path, settings, media, speaker="남자")[0]["status"] == "generated"
    assert media[0].calls[-1] == ("남자", "사과")


def test_failed_replacement_preserves_old_audio_and_retry_reuses_tts(audio_bank, tmp_path, settings, media):
    generate(audio_bank, tmp_path, settings, media)
    before = word(audio_bank)
    media[0].frequency = 550
    media[1].fail = True
    failed = generate(audio_bank, tmp_path, settings, media, regenerate=True)
    assert failed[0]["status"] == "failed" and "synthetic-secret" not in failed[0]["message"]
    assert word(audio_bank) == before
    assert len(media[0].calls) == 2
    media[1].fail = False
    assert generate(audio_bank, tmp_path, settings, media, regenerate=True)[0]["status"] == "generated"
    assert len(media[0].calls) == 2
    assert word(audio_bank)["audio_file_sha256"] != before["audio_file_sha256"]
    assert len(media[1].objects) == 2


def test_failed_save_reuses_generated_file_and_keeps_completed_words(audio_bank, tmp_path, settings, media, monkeypatch):
    original = audio._save_audio
    attempts = []

    def fail_first(c, before, values):
        attempts.append(before["vocabulary_id"])
        if before["vocabulary_id"] == 1:
            raise RuntimeError("synthetic-secret")
        return original(c, before, values)

    monkeypatch.setattr(audio, "_save_audio", fail_first)
    results = generate(audio_bank, tmp_path, settings, media, ids=(1, 2))
    assert [result["status"] for result in results] == ["failed", "generated"]
    assert word(audio_bank)["audio_storage_path"] is None
    monkeypatch.setattr(audio, "_save_audio", original)
    assert [row["status"] for row in generate(audio_bank, tmp_path, settings, media, ids=(1, 2))] == ["generated", "skipped"]
    assert len(media[0].calls) == 2


@pytest.mark.parametrize("column,value,expected", [("korean", "다른 말", "failed"), ("english", "new translation", "generated")])
def test_concurrent_word_change_blocks_save_but_translation_change_is_preserved(
        audio_bank, tmp_path, settings, media, column, value, expected):
    def update():
        with psycopg.connect(audio_bank) as c:
            c.execute(psycopg.sql.SQL("UPDATE topik_bank.vocabulary SET {}=%s WHERE vocabulary_id=1").format(
                psycopg.sql.Identifier(column)), (value,))

    media[1].on_verify = update
    result = generate(audio_bank, tmp_path, settings, media)[0]
    assert result["status"] == expected
    assert word(audio_bank)[column] == value
    if expected == "failed":
        assert word(audio_bank)["audio_storage_path"] is None


def test_session_lock_prevents_duplicate_tts(audio_bank, tmp_path, settings, media):
    with psycopg.connect(audio_bank, autocommit=True) as c:
        c.execute("SELECT pg_advisory_lock(hashtextextended('topik_bank:vocabulary_audio:1',0))")
        result = generate(audio_bank, tmp_path, settings, media)[0]
    assert result["status"] == "failed" and "다른 작업" in result["message"]
    assert not media[0].calls


def test_audio_filters_and_settings_changes(audio_bank, tmp_path, settings, media):
    generate(audio_bank, tmp_path, settings, media)
    profile = audio.audio_profile(settings)
    assert search_vocabulary(audio_bank)["audio_count"] == 1
    assert search_vocabulary(audio_bank, audio_filter="missing")["total"] == 54
    assert search_vocabulary(audio_bank, audio_filter="present", query="apple")["total"] == 1
    assert search_vocabulary(audio_bank, audio_filter="stale", audio_profile=profile)["total"] == 0
    assert search_vocabulary(audio_bank, audio_filter="stale", audio_profile=audio.audio_profile(settings, "남자"))["total"] == 1
    with psycopg.connect(audio_bank) as c:
        c.execute("UPDATE topik_bank.vocabulary SET korean='다른 말' WHERE vocabulary_id=1")
    assert search_vocabulary(audio_bank, audio_filter="stale", audio_profile=profile)["total"] == 1


@pytest.mark.parametrize("existing", [False, True])
def test_audio_transfer_upgrades_legacy_and_backs_up_complete_schema(banks, tmp_path, settings, media, existing, postgres_database_factory):
    source, target = banks
    if existing:
        send(source, target, tmp_path)
    audio.prepare_audio_schema(source, tmp_path)
    generate(source, tmp_path, settings, media)
    plan = prepare(source, target)
    assert plan.upgrade_audio is existing
    assert plan.effects["audio_created"] == 1
    result = send(source, target, tmp_path)
    assert stored(source) == stored(target)
    if existing:
        legacy_backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
        assert "audio_storage_path" not in legacy_backup["rows"][0]
    media[0].frequency = 660
    generate(source, tmp_path, settings, media, regenerate=True)
    assert prepare(source, target).effects["audio_updated"] == 1
    before = stored(target)
    result = send(source, target, tmp_path)
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    restored = postgres_database_factory()
    with psycopg.connect(restored) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute(backup["create_table_sql"])
        transfer._upsert(c, backup["rows"])
    assert stored(restored) == before
    assert stored(target) == stored(source)
    assert send(source, target, tmp_path)["unchanged"] == 4


def test_legacy_target_upgrade_rolls_back_on_verification_failure(banks, tmp_path, settings, media, monkeypatch):
    source, target = banks
    send(source, target, tmp_path)
    before = stored(target)
    audio.prepare_audio_schema(source, tmp_path)
    generate(source, tmp_path, settings, media)
    monkeypatch.setattr(transfer, "_verify", lambda *a: (_ for _ in ()).throw(RuntimeError("synthetic-secret")))
    with pytest.raises(transfer.TransferError):
        send(source, target, tmp_path)
    assert stored(target) == before


def test_source_without_audio_preserves_target_audio(banks, tmp_path, settings, media):
    source, target = banks
    audio.prepare_audio_schema(source, tmp_path)
    send(source, target, tmp_path)
    generate(target, tmp_path, settings, media)
    before = word(target)
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET english='updated translation' WHERE vocabulary_id=1")
    plan = prepare(source, target)
    assert plan.effects["audio_preserved"] == 1 and plan.effects["audio_updated"] == 0
    send(source, target, tmp_path)
    after = word(target)
    assert after["english"] == "updated translation"
    assert all(after[key] == before[key] for key in audio.AUDIO_FIELDS)


def test_ui_prepares_and_generates_selected_and_filtered_batch(bank, tmp_path, settings, media, monkeypatch):
    for key, value in (("GOOGLE_CLOUD_PROJECT_ID", settings.project), ("S3_BUCKET", settings.bucket),
                       ("CLOUDFRONT_BASE_URL", settings.base_url)):
        monkeypatch.setenv(key, value)
    calls = []

    def run(url, root, ids, **kwargs):
        calls.append(list(ids))
        kwargs["settings"] = replace(kwargs["settings"], tts_delay=0, retries=0)
        return audio.generate_vocabulary_audio(url, root, ids, client=media[0], cloud=media[1], **kwargs)

    monkeypatch.setattr(ui, "generate_vocabulary_audio", run)
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    assert not app.exception
    app.button(key="vocabulary-audio-prepare").click().run()
    assert not app.exception
    app.text_input(key="vocabulary-query").set_value("사과").run()
    app.button(key="vocabulary-audio-single").click().run()
    assert not app.exception and calls == [[1]]
    assert len(app.get("audio")) == 1
    assert app.button(key="vocabulary-audio-single").label == "선택 단어 음성 재생성"
    app.text_input(key="vocabulary-query").set_value("").run()
    app.selectbox(key="vocabulary-level").set_value(4).run()
    app.button(key="vocabulary-audio-batch").click().run()
    assert not app.exception and calls == [[1], [2]]
    assert app.button(key="vocabulary-audio-batch").disabled


def test_schema_rejects_partial_audio_metadata(audio_bank):
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(audio_bank) as c:
            c.execute("UPDATE topik_bank.vocabulary SET audio_storage_path='invalid.mp3' WHERE vocabulary_id=1")


def test_voice_settings_preserve_selection_and_survive_empty_results(audio_bank):
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.selectbox(key="vocabulary-detail").set_value(10).run()
    app.selectbox(key="vocabulary-audio-speaker").set_value("남자").run()
    app.number_input(key="vocabulary-audio-rate").set_value(0.9).run()
    assert not app.exception and app.selectbox(key="vocabulary-detail").value == 10
    app.text_input(key="vocabulary-query").set_value("no match").run()
    assert not app.exception
    assert not any(widget.key == "vocabulary-detail" for widget in app.selectbox)
    app.text_input(key="vocabulary-query").set_value("").run()
    assert not app.exception
    assert app.selectbox(key="vocabulary-audio-speaker").value == "남자"
    assert app.number_input(key="vocabulary-audio-rate").value == 0.9


def test_empty_batch_fails_before_network(tmp_path, settings, media):
    with pytest.raises(audio.VocabularyAudioError):
        generate("unused", tmp_path, settings, media, ids=[])
    assert not media[0].calls and not media[1].objects


def test_previous_word_prompt_requires_regeneration(settings):
    row = {"korean": "사과", "audio_storage_path": "previous.mp3"}
    previous = {**audio.generation_config(row, audio.audio_profile(settings)),
                "style_prompt": "", "version": "vocabulary_word_v1"}
    row.update(audio_generation_config=previous, audio_source_hash=audio.digest(previous))
    assert audio.audio_status(row, audio.audio_profile(settings)) == "stale"
