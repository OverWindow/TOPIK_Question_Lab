"""Vocabulary schema checks and recovery snapshots shared by audio and transfers."""
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
from psycopg import sql
from topik_question_lab.common.question_bank.connection import TransferError, digest
from topik_question_lab.common.paths import MIGRATION_DIR
from topik_question_lab.common.vocabulary.vocabulary_audio import AUDIO_FIELDS, AUDIO_TYPES, AUDIO_MIGRATION
from topik_question_lab.common.vocabulary.vocabulary_languages import LANGUAGE_FIELDS, LANGUAGE_MIGRATION

SCHEMA_FILE = MIGRATION_DIR / "012_vocabulary.sql"
FIELDS = ("vocabulary_id", "korean", "part_of_speech", "english", "vietnamese", "japanese",
          "example_korean", "example_english", "topik_level")


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
    language_columns = [(name, "text", True, "", "''::text") for name in LANGUAGE_FIELDS]
    allowed = [expected, expected + audio_columns, expected + language_columns,
               expected + audio_columns + language_columns]
    matched = next((candidate for candidate in allowed if sorted(columns) == sorted(candidate)), None)
    if matched is None:
        raise TransferError("단어장 컬럼·형식·기본값·자동 증가 구조가 지원 형식과 다릅니다.")
    # Feature migrations can be applied in either order on existing databases.
    columns = matched
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
    if any(column[0] == AUDIO_FIELDS[0] for column in columns):
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
            "columns": [row for row in schema["columns"] if row[0] not in AUDIO_FIELDS],
            "constraints": [row for row in schema["constraints"] if row != schema["audio_constraint"]]}


def _has_languages(schema):
    return schema is not None and any(row[0] == LANGUAGE_FIELDS[0] for row in schema["columns"])


def _rows(c):
    return [row[0] for row in c.execute("SELECT to_jsonb(v) FROM topik_bank.vocabulary v ORDER BY vocabulary_id")]


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
                   + ("\n" + AUDIO_MIGRATION.read_text(encoding="utf-8") if _has_audio(schema) else "")
                   + ("\n" + LANGUAGE_MIGRATION.read_text(encoding="utf-8") if _has_languages(schema) else "")}
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
