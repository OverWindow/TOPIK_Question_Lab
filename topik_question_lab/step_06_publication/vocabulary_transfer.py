"""Preview and copy the complete vocabulary to a separate server database."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from topik_question_lab.step_06_publication.bank_transfer import (
    TransferError, _distinct_target, connection, database_identity, digest,
)
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR
from topik_question_lab.step_06_publication.vocabulary_audio import (
    AUDIO_FIELDS, AUDIO_TYPES, AUDIO_MIGRATION, apply_audio_migration,
)


FIELDS = ("vocabulary_id", "korean", "part_of_speech", "english", "vietnamese", "japanese",
          "example_korean", "example_english", "topik_level")
SCHEMA_FILE = MIGRATION_DIR / "012_vocabulary.sql"


def _schema(c):
    relation = c.execute("""SELECT relkind,relrowsecurity,relforcerowsecurity FROM pg_class
        WHERE oid=to_regclass('topik_bank.vocabulary')""").fetchone()
    if relation is None:
        return None
    if relation != ("r", False, False):
        raise TransferError("단어장에는 일반 테이블과 행 보안이 없는 구조가 필요합니다.")
    if c.execute("""SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='topik_bank.vocabulary'::regclass
        AND NOT tgisinternal AND tgenabled<>'D')""").fetchone()[0]:
        raise TransferError("단어장에 사용자 트리거가 있어 자동 등록할 수 없습니다.")
    columns = c.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,
        pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='topik_bank.vocabulary'::regclass AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY a.attnum""").fetchall()
    expected = [(name, "bigint" if name == "vocabulary_id" else "smallint" if name == "topik_level" else "text",
                 True, "a" if name == "vocabulary_id" else "", "''::text" if name == "part_of_speech" else None)
                for name in FIELDS]
    audio_columns = [(name, kind, False, "", None) for name, kind in AUDIO_TYPES.items()]
    if columns not in (expected, expected + audio_columns):
        raise TransferError("단어장 컬럼·형식·기본값·자동 증가 구조가 지원 형식과 다릅니다.")
    # PostgreSQL 18 also catalogs NOT NULL here; attnotnull above checks it across versions.
    constraints = c.execute("""SELECT pg_get_constraintdef(oid),convalidated FROM pg_constraint
        WHERE conrelid='topik_bank.vocabulary'::regclass AND contype <> 'n' ORDER BY 1""").fetchall()
    indexes = c.execute("""SELECT pg_get_indexdef(indexrelid),indisvalid,indisready FROM pg_index
        WHERE indrelid='topik_bank.vocabulary'::regclass ORDER BY 1""").fetchall()
    sequence = c.execute("""SELECT seqincrement,seqmin,seqmax,seqcache,seqcycle FROM pg_sequence
        WHERE seqrelid=pg_get_serial_sequence('topik_bank.vocabulary','vocabulary_id')::regclass""").fetchone()
    if sequence != (1, 1, 9223372036854775807, 1, False):
        raise TransferError("단어장의 자동 증가 번호 설정이 지원 형식과 다릅니다.")
    # Sort in Python so different database collations yield the same description.
    description = {"columns": [list(row) for row in columns], "constraints": sorted(list(row) for row in constraints),
                   "indexes": sorted([definition.split(" USING ", 1)[1], valid, ready] for definition, valid, ready in indexes),
                   "sequence": list(sequence)}
    if len(columns) > len(expected):
        constraint = c.execute("""SELECT pg_get_constraintdef(oid),convalidated FROM pg_constraint
            WHERE conrelid='topik_bank.vocabulary'::regclass AND conname='vocabulary_audio_complete'""").fetchone()
        if constraint is None or not constraint[1]:
            raise TransferError("단어장 음성의 필수 제약을 확인할 수 없습니다.")
        description["audio_constraint"] = list(constraint)
    return description


def _has_audio(schema):
    return schema is not None and "audio_constraint" in schema


def _base_schema(schema):
    if not _has_audio(schema):
        return schema
    return {**{key: value for key, value in schema.items() if key != "audio_constraint"},
            "columns": schema["columns"][:len(FIELDS)],
            "constraints": [row for row in schema["constraints"] if row != schema["audio_constraint"]]}


def _with_audio(rows):
    return [{**dict.fromkeys(AUDIO_FIELDS), **row} for row in rows]


def _effective_rows(source, target):
    by_id = {row["vocabulary_id"]: row for row in target}
    result = []
    for row in source:
        old = by_id.get(row["vocabulary_id"], {})
        if "audio_storage_path" in row and not row["audio_storage_path"] and old.get("audio_storage_path"):
            row = {**row, **{key: old[key] for key in AUDIO_FIELDS}}
        result.append(row)
    return result


def _rows(c):
    return [row[0] for row in c.execute("SELECT to_jsonb(v) FROM topik_bank.vocabulary v ORDER BY vocabulary_id")]


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
    by_id = {row["vocabulary_id"]: row for row in target}
    preserved = sum("audio_storage_path" in row and not row["audio_storage_path"]
                    and bool(by_id.get(row["vocabulary_id"], {}).get("audio_storage_path")) for row in source)
    source = _effective_rows(source, target)
    by_word = {(row["korean"], row["part_of_speech"]): row for row in target}
    result = {"created": 0, "updated": 0, "unchanged": 0, "conflicts": []}
    for row in source:
        old = by_id.get(row["vocabulary_id"])
        word = by_word.get((row["korean"], row["part_of_speech"]))
        if old and (old["korean"], old["part_of_speech"]) != (row["korean"], row["part_of_speech"]):
            reason = "같은 ID가 다른 단어·품사에 사용 중"
        elif word and word["vocabulary_id"] != row["vocabulary_id"]:
            reason = "같은 단어·품사의 ID가 다름"
        else:
            result["created" if old is None else "unchanged" if old == row else "updated"] += 1
            continue
        result["conflicts"].append({"ID": row["vocabulary_id"], "단어": row["korean"],
                                    "품사": row["part_of_speech"] or "미지정", "사유": reason})
    ids = {row["vocabulary_id"] for row in source}
    result["extra"] = sum(row["vocabulary_id"] not in ids for row in target)
    if audio:
        result["audio_preserved"] = preserved
        result["audio_created"] = sum(bool(row["audio_storage_path"]) and not by_id.get(row["vocabulary_id"], {}).get("audio_storage_path")
                                      for row in source)
        result["audio_updated"] = sum(bool(by_id.get(row["vocabulary_id"], {}).get("audio_storage_path"))
                                      and any(by_id[row["vocabulary_id"]].get(key) != row.get(key) for key in AUDIO_FIELDS)
                                      for row in source)
    return result


def prepare_vocabulary_transfer(snapshot, target_url):
    with connection(target_url) as c:
        identity = _distinct_target(c, snapshot.identity)
        schema = _schema(c)
        upgrade = schema is not None and _has_audio(snapshot.schema) and not _has_audio(schema)
        expected_schema = _base_schema(snapshot.schema) if upgrade else snapshot.schema
        if schema is not None and schema != expected_schema:
            raise TransferError("대상 단어장의 제약·인덱스 구조가 원본과 다릅니다. 기존 구조를 확인하세요.")
        rows = _rows(c) if schema else []
        return VocabularyTransferPlan(snapshot, identity, digest((schema, rows)), schema is None,
                                      _effects(snapshot.rows, rows), len(rows), _levels(rows), upgrade)


def _sequence_state(c):
    schema, name = c.execute("""SELECT n.nspname,s.relname FROM pg_class s
        JOIN pg_namespace n ON n.oid=s.relnamespace
        WHERE s.oid=pg_get_serial_sequence('topik_bank.vocabulary','vocabulary_id')::regclass""").fetchone()
    value, called = c.execute(sql.SQL("SELECT last_value,is_called FROM {}.{}").format(
        sql.Identifier(schema), sql.Identifier(name))).fetchone()
    return {"schema": schema, "name": name, "last_value": value, "is_called": called}


def _backup(root, identity, schema, rows, sequence):
    payload = {"format": "topik-vocabulary-backup-v1", "target_fingerprint": digest(identity),
               "created_at": datetime.now(timezone.utc).isoformat(),
               "schema": schema, "rows": rows, "sequence": sequence,
               "create_table_sql": SCHEMA_FILE.read_text(encoding="utf-8")
                   + ("\n" + AUDIO_MIGRATION.read_text(encoding="utf-8") if _has_audio(schema) else "")}
    payload["content_hash"] = digest((schema, rows, sequence))
    directory = Path(root) / "backups" / "vocabulary"
    path = directory / f"before_transfer_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError("backup mismatch")
    except (OSError, ValueError):
        raise TransferError("변경 전 단어장 백업에 실패했습니다. DB는 변경하지 않았습니다.") from None
    return path


def _upsert(c, rows):
    if not rows:
        return
    fields = FIELDS + AUDIO_FIELDS if "audio_storage_path" in rows[0] else FIELDS
    names = sql.SQL(",").join(map(sql.Identifier, fields))
    updates = sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(name), sql.Identifier(name))
                              for name in fields[3:])
    c.execute(sql.SQL("""INSERT INTO topik_bank.vocabulary ({names}) OVERRIDING SYSTEM VALUE
        SELECT {names} FROM jsonb_populate_recordset(NULL::topik_bank.vocabulary,%s)
        ON CONFLICT (vocabulary_id) DO UPDATE SET {updates}""").format(names=names, updates=updates), (Jsonb(rows),))


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
            if effects["conflicts"]:
                raise TransferError("단어장 ID·단어·품사 충돌을 해결한 후 다시 확인하세요.")
            normalized = _with_audio(previous) if _has_audio(fresh.schema) else previous
            effective = _effective_rows(fresh.rows, normalized)
            expected = {row["vocabulary_id"]: row for row in normalized}
            expected.update({row["vocabulary_id"]: row for row in effective})
            expected = sorted(expected.values(), key=lambda row: row["vocabulary_id"])
            sequence = _sequence_state(c) if schema else None
            current_next = sequence["last_value"] + int(sequence["is_called"]) if sequence else 1
            next_id = max(current_next, max(row["vocabulary_id"] for row in expected) + 1)
            changes = schema is None or plan.upgrade_audio or effects["created"] or effects["updated"] or current_next != next_id
            if changes:
                if schema:
                    backup = _backup(root, plan.target_identity, schema, previous, sequence)
                else:
                    c.execute("CREATE SCHEMA IF NOT EXISTS topik_bank")
                    c.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
                if _has_audio(fresh.schema) and not _has_audio(schema):
                    apply_audio_migration(c)
                if _schema(c) != fresh.schema:
                    raise TransferError("원본 단어장이 지원하는 생성 구조와 다릅니다.")
                by_id = {row["vocabulary_id"]: row for row in normalized}
                _upsert(c, [row for row in effective if by_id.get(row["vocabulary_id"]) != row])
                # RESTART is transactional; setval would survive a later rollback.
                if current_next != next_id:
                    c.execute(sql.SQL("ALTER TABLE topik_bank.vocabulary ALTER COLUMN vocabulary_id RESTART WITH {}")
                              .format(sql.Literal(next_id)))
            _verify(c, expected, fresh.schema)
            commit_attempted = True
        committed = True
        with connection(target_url) as c:
            if _distinct_target(c, fresh.identity) != plan.target_identity:
                raise TransferError("대상 DB가 변경되었습니다.")
            _verify(c, expected, fresh.schema)
        return {"status": "verified", "created": effects["created"], "updated": effects["updated"],
                "unchanged": effects["unchanged"], "extra": effects["extra"], "source_count": len(fresh.rows),
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
