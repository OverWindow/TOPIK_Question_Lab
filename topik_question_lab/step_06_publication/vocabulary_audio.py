"""Generate and verify one Korean word at a time before replacing its audio."""
from __future__ import annotations

from contextlib import suppress
import json
from pathlib import Path
import subprocess
import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from topik_question_lab.step_03_generation.audio_composer import pcm, validate_mp3
from topik_question_lab.step_03_generation.media_provider import GoogleMediaClient, MediaSettings, RequestRunner
from topik_question_lab.step_06_publication.bank_transfer import connection, database_identity, digest
from topik_question_lab.step_06_publication.media_cloud import S3MediaStore, cloudfront_url, destination_key, digest_file
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR


AUDIO_TYPES = {
    "audio_storage_bucket": "text", "audio_storage_path": "text", "audio_file_sha256": "text",
    "audio_byte_size": "integer", "audio_duration_ms": "integer", "audio_source_hash": "text",
    "audio_generation_config": "jsonb", "audio_created_at": "timestamp with time zone",
}
AUDIO_FIELDS = tuple(AUDIO_TYPES)
AUDIO_MIGRATION = MIGRATION_DIR / "014_vocabulary_audio.sql"
MAX_BATCH_SIZE = 100
WORD_NARRATION_PROMPT = (
    "한국어 학습용 사전의 표제어를 읽습니다. 표준 한국어로, 중립적이고 차분한 목소리로 낭독하세요. "
    "보통 높이의 음역에서 억양 변화와 강세를 작게 하고, 끝을 가볍게 내려 마무리하세요. "
    "음절을 끊거나 끝소리를 늘이지 말고, 자연스럽고 고른 속도로 읽으세요. "
    "입력된 단어만 정확히 한 번 읽고 다른 말이나 소리를 덧붙이지 마세요."
)


class VocabularyAudioError(ValueError):
    pass


def audio_profile(settings: MediaSettings, speaker: str = "여자") -> dict:
    if speaker not in ("여자", "남자"):
        raise VocabularyAudioError("음성 화자를 선택하세요.")
    return {"provider": "google_cloud_tts", "model": settings.tts_model,
            "voice": settings.female if speaker == "여자" else settings.male, "speaker": speaker,
            "speaking_rate": settings.speaking_rate, "style_prompt": WORD_NARRATION_PROMPT,
            "version": "vocabulary_word_v2"}


def generation_config(row: dict, profile: dict) -> dict:
    return {**profile, "text": row["korean"].strip()}


def audio_status(row: dict, profile: dict | None = None) -> str:
    if not row.get("audio_storage_path"):
        return "missing"
    saved = row.get("audio_generation_config") or {}
    expected = generation_config(row, profile if profile is not None else saved)
    return "ready" if saved == expected and row.get("audio_source_hash") == digest(expected) else "stale"


def apply_audio_migration(c):
    c.execute(AUDIO_MIGRATION.read_text(encoding="utf-8"))
    if c.execute("SELECT to_regclass('topik_bank.schema_migrations')").fetchone()[0]:
        c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
                  (AUDIO_MIGRATION.stem,))


def prepare_audio_schema(database_url: str, root: Path) -> Path | None:
    from topik_question_lab.step_06_publication.vocabulary_transfer import (
        _backup, _has_audio, _rows, _schema, _sequence_state, _base_schema,
    )

    backup = None
    try:
        with connection(database_url, readonly=False) as c:
            for name in ("schema_migrations", "vocabulary_import"):
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("topik_bank:" + name,))
            before_schema = _schema(c)
            if before_schema is None:
                raise VocabularyAudioError("단어장 CSV를 먼저 등록하세요.")
            c.execute("LOCK TABLE topik_bank.vocabulary IN SHARE ROW EXCLUSIVE MODE")
            before_schema = _schema(c)
            if _has_audio(before_schema):
                return None
            before = _rows(c)
            backup = _backup(root, database_identity(c), before_schema, before, _sequence_state(c))
            apply_audio_migration(c)
            after = _rows(c)
            if (_base_schema(_schema(c)) != before_schema
                    or [{k: v for k, v in row.items() if k not in AUDIO_FIELDS} for row in after] != before
                    or any(row[k] is not None for row in after for k in AUDIO_FIELDS)):
                raise VocabularyAudioError("음성 컬럼 준비 후 기존 단어장 검증에 실패했습니다.")
        with connection(database_url) as c:
            if not _has_audio(_schema(c)):
                raise VocabularyAudioError("음성 컬럼 준비 결과를 확인하지 못했습니다.")
        return backup
    except Exception:
        saved = f" 백업: {backup}" if backup else ""
        raise VocabularyAudioError("음성 기능 준비 결과를 확인하지 못했습니다. DB 상태를 확인하고 다시 시도하세요." + saved) from None


def _cached_audio(directory: Path, config: dict, previous_hash, settings, client, runner):
    # The previous file distinguishes a replacement from the initial generation.
    key = digest((config, previous_hash))
    directory.mkdir(parents=True, exist_ok=True)
    target, manifest, speech = (directory / f"{key}{suffix}" for suffix in (".mp3", ".json", ".wav"))
    if manifest.is_file() and target.is_file():
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            if (metadata["config"] == config
                    and digest_file(target) == (metadata["byte_size"], metadata["file_sha256"])):
                duration = validate_mp3(target, settings)
                return target, duration, (target, manifest, speech)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def utterance(speaker, text):
        if speech.is_file():
            try:
                data = speech.read_bytes()
                pcm(data)
                return data
            except (OSError, ValueError):
                pass
        data = runner.call("tts", lambda: client.speech(speaker, text, prompt=config["style_prompt"]))
        pcm(data)
        temporary = speech.with_name(f"{key}.{uuid.uuid4().hex[:8]}.wav")
        temporary.write_bytes(data)
        temporary.replace(speech)
        return data

    # Keep the entire headword in one request, including punctuation in dictionary entries.
    utterance(config["speaker"], config["text"])
    temporary = target.with_name(f"{key}.{uuid.uuid4().hex[:8]}.part.mp3")
    encoded = subprocess.run([settings.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(speech),
                              "-codec:a", "libmp3lame", "-b:a", "64k", str(temporary)],
                             capture_output=True, timeout=120,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if encoded.returncode:
        raise VocabularyAudioError("단어 음성의 MP3 변환에 실패했습니다.")
    duration = validate_mp3(temporary, settings)
    temporary.replace(target)
    size, checksum = digest_file(target)
    metadata = {"config": config, "byte_size": size, "file_sha256": checksum}
    temporary_manifest = manifest.with_suffix(".part.json")
    temporary_manifest.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    temporary_manifest.replace(manifest)
    return target, duration, (target, manifest, speech)


def _save_audio(c, before, values):
    with c.transaction():
        current = c.execute("SELECT to_jsonb(v) FROM topik_bank.vocabulary v WHERE vocabulary_id=%s FOR UPDATE",
                            (before["vocabulary_id"],)).fetchone()
        watched = ("korean", "part_of_speech", *AUDIO_FIELDS)
        if current is None or any(current[0].get(key) != before.get(key) for key in watched):
            raise VocabularyAudioError("생성 중 단어나 음성이 변경되었습니다. 새로고침 후 다시 시도하세요.")
        assignments = sql.SQL(", ").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in values)
        c.execute(sql.SQL("UPDATE topik_bank.vocabulary SET {}, audio_created_at=CURRENT_TIMESTAMP "
                          "WHERE vocabulary_id=%s").format(assignments),
                  [Jsonb(value) if key == "audio_generation_config" else value for key, value in values.items()]
                  + [before["vocabulary_id"]])
    actual = c.execute("SELECT to_jsonb(v) FROM topik_bank.vocabulary v WHERE vocabulary_id=%s",
                       (before["vocabulary_id"],)).fetchone()
    if actual is None or any(actual[0].get(key) != value for key, value in values.items()):
        raise VocabularyAudioError("저장 결과를 확인하지 못했습니다. 새로고침 후 음성을 확인하세요.")


def generate_vocabulary_audio(database_url: str, root: Path, vocabulary_ids, *, settings=None,
                              speaker="여자", regenerate=False, progress=None, client=None, cloud=None):
    ids = list(dict.fromkeys(vocabulary_ids))
    if not ids or len(ids) > MAX_BATCH_SIZE or any(type(value) is not int or value < 1 for value in ids):
        raise VocabularyAudioError(f"한 번에 1~{MAX_BATCH_SIZE}개의 단어를 선택하세요.")
    settings = settings or MediaSettings.from_env()
    profile = audio_profile(settings, speaker)
    owned_client, owned_cloud = client is None, cloud is None
    results = []
    try:
        settings.validate("listening")
        client = client or GoogleMediaClient(settings)
        cloud = cloud or S3MediaStore(settings.bucket, settings.base_url, region=settings.region)
        runner = RequestRunner(settings)
        for index, vocabulary_id in enumerate(ids):
            result = {"vocabulary_id": vocabulary_id, "word": "", "status": "failed", "message": ""}
            try:
                # A session lock prevents duplicate paid requests without a long SQL transaction.
                with psycopg.connect(database_url, autocommit=True, connect_timeout=8,
                                     options="-c timezone=UTC -c statement_timeout=15000 -c lock_timeout=8000") as c:
                    locked = c.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                                       (f"topik_bank:vocabulary_audio:{vocabulary_id}",)).fetchone()[0]
                    if not locked:
                        raise VocabularyAudioError("이 단어의 음성을 다른 작업에서 생성 중입니다.")
                    found = c.execute("SELECT to_jsonb(v) FROM topik_bank.vocabulary v WHERE vocabulary_id=%s",
                                      (vocabulary_id,)).fetchone()
                    if found is None:
                        raise VocabularyAudioError("단어를 찾을 수 없습니다. 새로고침하세요.")
                    row = found[0]
                    result["word"] = row["korean"]
                    if not all(field in row for field in AUDIO_FIELDS):
                        raise VocabularyAudioError("단어장 화면에서 음성 기능을 먼저 준비하세요.")
                    if not regenerate and audio_status(row, profile) == "ready":
                        result.update(status="skipped", message="같은 설정의 음성이 이미 있습니다.")
                    else:
                        config = generation_config(row, profile)
                        if progress:
                            progress(index, len(ids), row["korean"], "음성 생성·변환 중")
                        # Keep paths short enough for FFmpeg on Windows.
                        namespace = digest((database_identity(c), vocabulary_id))[:24]
                        directory = Path(root) / "data" / "vocabulary_audio" / namespace
                        path, duration, cache_files = _cached_audio(
                            directory, config, row.get("audio_file_sha256") if regenerate else None,
                            settings, client, runner)
                        size, checksum = digest_file(path)
                        key = destination_key("vocabulary-audio", checksum, "audio/mpeg")
                        media = {"storage_bucket": cloud.bucket, "storage_path": key,
                                 "storage_url": cloudfront_url(settings.base_url, key),
                                 "byte_size": size, "file_sha256": checksum, "mime_type": "audio/mpeg"}
                        if progress:
                            progress(index, len(ids), row["korean"], "S3 업로드·재생 검증 중")
                        runner.call("upload", lambda: cloud.upload(path, key, size, checksum, "audio/mpeg"))
                        runner.call("verify", lambda: cloud.verify(media, local_path=path))
                        values = {"audio_" + name: media[name] for name in
                                  ("storage_bucket", "storage_path", "file_sha256", "byte_size")}
                        values.update(audio_duration_ms=duration, audio_source_hash=digest(config),
                                      audio_generation_config=config)
                        _save_audio(c, row, values)
                        result.update(status="generated", message="S3 저장·재생 검증 완료")
                        for cached in cache_files:
                            with suppress(OSError):
                                cached.unlink(missing_ok=True)
            except Exception as exc:
                result["message"] = str(exc) if isinstance(exc, VocabularyAudioError) else (
                    "음성 생성·저장 결과를 확인하지 못했습니다. 설정과 연결을 확인하고 다시 시도하세요.")
            results.append(result)
            if progress:
                progress(index + 1, len(ids), result["word"], result["message"])
        return results
    except Exception:
        raise VocabularyAudioError("음성 생성을 시작하지 못했습니다. Google TTS·S3·CloudFront·FFmpeg 설정을 확인하세요.") from None
    finally:
        if owned_client and client is not None:
            client.close()
        if owned_cloud and cloud is not None:
            cloud.close()
