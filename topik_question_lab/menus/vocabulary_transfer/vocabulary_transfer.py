"""Preview and copy the complete vocabulary to a separate server database."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from topik_question_lab.common.question_bank.connection import TransferError, _distinct_target, connection, database_identity, digest
from topik_question_lab.common.question_bank.postgres_storage import MIGRATION_DIR
from topik_question_lab.common.vocabulary.vocabulary_audio import AUDIO_FIELDS, AUDIO_TYPES, AUDIO_MIGRATION, apply_audio_migration
from topik_question_lab.common.vocabulary.vocabulary_backup import (
    FIELDS, _schema, _has_audio, _base_schema, _has_languages, _rows, _sequence_state, _backup,
)
from topik_question_lab.common.vocabulary.vocabulary_languages import LANGUAGE_FIELDS, LANGUAGE_MIGRATION, apply_language_migration


SCHEMA_FILE = MIGRATION_DIR / "012_vocabulary.sql"


def _without_languages(schema):
    return {**schema, "columns": [row for row in schema["columns"] if row[0] not in LANGUAGE_FIELDS]}


def _with_languages(rows):
    return [{**dict.fromkeys(LANGUAGE_FIELDS, ""), **row} for row in rows]


def _with_audio(rows):
    return [{**dict.fromkeys(AUDIO_FIELDS), **row} for row in rows]


def _levels(rows):
    counts = Counter(row["topik_level"] for row in rows)
    return {level: counts[level] for level in range(3, 7)}


@dataclass
class VocabularySnapshot:
    identity: dict
    schema: dict
    rows: list[dict]

    @property
    def signature(self):
        return digest((self.identity, self.schema, self.rows))

    @property
    def levels(self):
        return _levels(self.rows)


@dataclass
class VocabularyTransferPlan:
    snapshot: VocabularySnapshot
    target_identity: dict
    target_signature: str
    create_table: bool
    effects: dict
    target_count: int
    target_levels: dict
    upgrade_audio: bool = False
    upgrade_languages: bool = False
    question_signature: str | None = None
    deleted_questions: int = 0


def capture_vocabulary(source_url):
    with connection(source_url) as c:
        schema = _schema(c)
        if schema is None:
            raise TransferError("생성기 원본에 단어장이 없습니다. CSV를 먼저 등록하세요.")
        rows = _rows(c)
        if not rows:
            raise TransferError("생성기 원본에 등록할 단어가 없습니다.")
        return VocabularySnapshot(database_identity(c), schema, rows)


def _effects(source, target):
    audio = bool(source and "audio_storage_path" in source[0])
    if audio:
        target = _with_audio(target)
    if source and LANGUAGE_FIELDS[0] in source[0]:
        target = _with_languages(target)
    by_id = {row["vocabulary_id"]: row for row in target}
    result = {"created": 0, "updated": 0, "unchanged": 0}
    for row in source:
        old = by_id.get(row["vocabulary_id"])
        result["created" if old is None else "unchanged" if old == row else "updated"] += 1
    ids = {row["vocabulary_id"] for row in source}
    result["deleted"] = sum(row["vocabulary_id"] not in ids for row in target)
    if audio:
        result["audio_created"] = sum(bool(row["audio_storage_path"]) and not by_id.get(row["vocabulary_id"], {}).get("audio_storage_path")
                                      for row in source)
        result["audio_updated"] = sum(bool(by_id.get(row["vocabulary_id"], {}).get("audio_storage_path"))
                                      and any(by_id[row["vocabulary_id"]].get(key) != row.get(key) for key in AUDIO_FIELDS)
                                      for row in source)
    return result


def prepare_vocabulary_transfer(snapshot, target_url):
    with connection(target_url) as c:
        plan = _prepare_vocabulary_transfer(c, snapshot)
        if plan.effects["deleted"]:
            from topik_question_lab.menus.vocabulary_transfer import vocabulary_question_transfer as questions
            schema = questions._schema(c)
            rows = questions._rows(c) if schema else []
            ids = {row["vocabulary_id"] for row in snapshot.rows}
            plan.question_signature = digest((schema, rows))
            plan.deleted_questions = sum(row["vocabulary_id"] not in ids for row in rows)
        return plan


def _prepare_vocabulary_transfer(c, snapshot):
    identity = _distinct_target(c, snapshot.identity)
    schema = _schema(c)
    upgrade = schema is not None and _has_audio(snapshot.schema) and not _has_audio(schema)
    upgrade_languages = schema is not None and _has_languages(snapshot.schema) and not _has_languages(schema)
    expected_schema = _base_schema(snapshot.schema) if upgrade else snapshot.schema
    if upgrade_languages:
        expected_schema = _without_languages(expected_schema)
    if schema is not None and schema != expected_schema:
        raise TransferError("대상 단어장의 제약·인덱스 구조가 원본과 다릅니다. 기존 구조를 확인하세요.")
    rows = _rows(c) if schema else []
    return VocabularyTransferPlan(snapshot, identity, digest((schema, rows)), schema is None,
                                  _effects(snapshot.rows, rows), len(rows), _levels(rows), upgrade, upgrade_languages)


def _upsert(c, rows):
    if not rows:
        return
    fields = FIELDS + AUDIO_FIELDS if "audio_storage_path" in rows[0] else FIELDS
    if LANGUAGE_FIELDS[0] in rows[0]:
        fields += LANGUAGE_FIELDS
    names = sql.SQL(",").join(map(sql.Identifier, fields))
    updates = sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(name), sql.Identifier(name))
                              for name in fields[1:])
    c.execute(sql.SQL("""INSERT INTO topik_bank.vocabulary ({names}) OVERRIDING SYSTEM VALUE
        SELECT {names} FROM jsonb_populate_recordset(NULL::topik_bank.vocabulary,%s)
        ON CONFLICT (vocabulary_id) DO UPDATE SET {updates}""").format(names=names, updates=updates), (Jsonb(rows),))


def _sync_rows(c, rows, previous):
    by_id = {row["vocabulary_id"]: row for row in previous}
    ids = [row["vocabulary_id"] for row in rows]
    c.execute("DELETE FROM topik_bank.vocabulary WHERE NOT(vocabulary_id=ANY(%s))", (ids,))
    renamed = [row["vocabulary_id"] for row in rows if row["vocabulary_id"] in by_id
               and (row["korean"], row["part_of_speech"]) !=
                   (by_id[row["vocabulary_id"]]["korean"], by_id[row["vocabulary_id"]]["part_of_speech"])]
    if renamed:
        # Release unique word/POS keys before swaps; IDs and references stay intact.
        c.execute("UPDATE topik_bank.vocabulary SET part_of_speech=%s || vocabulary_id::text WHERE vocabulary_id=ANY(%s)",
                  ("__sync_" + uuid.uuid4().hex, renamed))
    _upsert(c, [row for row in rows if by_id.get(row["vocabulary_id"]) != row])


def _verify(c, expected, schema):
    if _schema(c) != schema or _rows(c) != expected:
        raise TransferError("등록된 단어장 구조 또는 전체 내용이 예상 결과와 다릅니다.")
    sequence = _sequence_state(c)
    if sequence["last_value"] + int(sequence["is_called"]) <= max(row["vocabulary_id"] for row in expected):
        raise TransferError("단어장의 다음 자동 생성 번호를 확인하지 못했습니다.")


def execute_vocabulary_transfer(source_url, target_url, plan, root):
    fresh = capture_vocabulary(source_url)
    if fresh.signature != plan.snapshot.signature:
        raise TransferError("미리보기 이후 원본 단어장이 변경되었습니다. 등록 내용을 다시 확인하세요.")
    backup = None
    commit_attempted = False
    committed = False
    try:
        with connection(target_url, readonly=False) as c:
            if _distinct_target(c, fresh.identity) != plan.target_identity:
                raise TransferError("대상 DB가 변경되었습니다. 등록 내용을 다시 확인하세요.")
            for lock in ("schema_migrations", "vocabulary_import"):
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("topik_bank:" + lock,))
            schema = _schema(c)
            if schema is not None:
                c.execute("LOCK TABLE topik_bank.vocabulary IN SHARE ROW EXCLUSIVE MODE")
                schema = _schema(c)
            previous = _rows(c) if schema else []
            if digest((schema, previous)) != plan.target_signature:
                raise TransferError("미리보기 이후 대상 단어장이 변경되었습니다. 등록 내용을 다시 확인하세요.")
            effects = _effects(fresh.rows, previous)
            expected = fresh.rows
            question_schema, previous_questions, remaining_questions = None, [], []
            if plan.question_signature is not None:
                from topik_question_lab.menus.vocabulary_transfer import vocabulary_question_transfer as questions
                question_schema = questions._schema(c)
                if question_schema:
                    c.execute("LOCK TABLE topik_bank.vocabulary_questions IN SHARE ROW EXCLUSIVE MODE")
                    question_schema = questions._schema(c)
                    previous_questions = questions._rows(c)
                if digest((question_schema, previous_questions)) != plan.question_signature:
                    raise TransferError("미리보기 이후 대상 문제가 변경되었습니다. 등록 내용을 다시 확인하세요.")
                ids = {row["vocabulary_id"] for row in fresh.rows}
                remaining_questions = [row for row in previous_questions if row["vocabulary_id"] in ids]
            sequence = _sequence_state(c) if schema else None
            current_next = sequence["last_value"] + int(sequence["is_called"]) if sequence else 1
            next_id = max(current_next, max(row["vocabulary_id"] for row in expected) + 1)
            changes = (schema is None or plan.upgrade_audio or plan.upgrade_languages
                       or effects["created"] or effects["updated"] or effects["deleted"] or current_next != next_id)
            if changes:
                if question_schema:
                    backup = questions._backup(root, plan.target_identity, schema, previous, sequence,
                                               question_schema, previous_questions, questions._sequence_state(c))
                elif schema:
                    backup = _backup(root, plan.target_identity, schema, previous, sequence)
                else:
                    c.execute("CREATE SCHEMA IF NOT EXISTS topik_bank")
                    c.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
                if _has_audio(fresh.schema) and not _has_audio(schema):
                    apply_audio_migration(c)
                if _has_languages(fresh.schema) and not _has_languages(schema):
                    apply_language_migration(c)
                if _schema(c) != fresh.schema:
                    raise TransferError("원본 단어장이 지원하는 생성 구조와 다릅니다.")
                if question_schema:
                    c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE NOT(vocabulary_id=ANY(%s))",
                              ([row["vocabulary_id"] for row in fresh.rows],))
                _sync_rows(c, fresh.rows, previous)
                # RESTART is transactional; setval would survive a later rollback.
                if current_next != next_id:
                    c.execute(sql.SQL("ALTER TABLE topik_bank.vocabulary ALTER COLUMN vocabulary_id RESTART WITH {}")
                              .format(sql.Literal(next_id)))
            _verify(c, expected, fresh.schema)
            if question_schema and questions._rows(c) != remaining_questions:
                raise TransferError("연결 문제 삭제 결과가 예상 내용과 다릅니다.")
            commit_attempted = True
        committed = True
        with connection(target_url) as c:
            if _distinct_target(c, fresh.identity) != plan.target_identity:
                raise TransferError("대상 DB가 변경되었습니다.")
            _verify(c, expected, fresh.schema)
            if question_schema and questions._rows(c) != remaining_questions:
                raise TransferError("연결 문제 삭제 결과가 예상 내용과 다릅니다.")
        return {"status": "verified", "created": effects["created"], "updated": effects["updated"],
                "unchanged": effects["unchanged"], "deleted": effects["deleted"],
                "deleted_questions": plan.deleted_questions, "source_count": len(fresh.rows),
                "target_count": len(expected), "levels": fresh.levels, "created_table": schema is None,
                "backup": str(backup) if backup else None}
    except Exception as exc:
        if committed:
            message = "등록은 커밋되었지만 최종 조회 검증을 완료하지 못했습니다. 등록 내용을 다시 확인하세요."
        elif commit_attempted:
            message = "커밋 결과를 확정하지 못했습니다. 등록 내용을 다시 확인하세요."
        else:
            message = str(exc) if isinstance(exc, TransferError) else "단어장 등록에 실패해 변경을 롤백했습니다."
        if backup:
            message += f" 백업: {backup}"
        raise TransferError(message) from None
