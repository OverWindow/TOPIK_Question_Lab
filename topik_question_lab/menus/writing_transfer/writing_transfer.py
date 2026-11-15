"""Copy selected writing sets to a distinct database, preserving their IDs."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from topik_question_lab.common.paths import MIGRATION_DIR
from topik_question_lab.common.question_bank.connection import (
    TransferError, _distinct_target, connection, database_identity, digest,
)
from topik_question_lab.common.writing_models import WritingQuestion


SCHEMA_FILE = MIGRATION_DIR / "018_writing_sets.sql"
FIELDS = {
    "writing_sets": ("set_id", "source_key", "exam_format", "provider", "model", "created_at", "updated_at"),
    "writing_questions": ("set_id", "type_slot", "source_generated_id", "content_json"),
}
KEYS = {"writing_sets": ("set_id",), "writing_questions": ("set_id", "type_slot")}


def _schema(c):
    # Stable qualification makes schema comparisons independent of role search paths.
    c.execute("SET LOCAL search_path = pg_catalog")
    relations = c.execute("""SELECT relname,relkind,relrowsecurity,relforcerowsecurity FROM pg_class
        WHERE relnamespace=to_regnamespace('topik_bank') AND relname=ANY(%s) ORDER BY relname""",
        (list(FIELDS),)).fetchall()
    if not relations:
        return None
    if len(relations) != 2 or any(row[1:] != ("r", False, False) for row in relations):
        raise TransferError("쓰기 테이블 2개가 모두 필요하며 일반 테이블 구조여야 합니다. 기존 구조를 확인하세요.")
    result = {}
    for table, fields in FIELDS.items():
        relation = f"topik_bank.{table}"
        if c.execute("""SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=%s::regclass
            AND NOT tgisinternal AND tgenabled<>'D')""", (relation,)).fetchone()[0]:
            raise TransferError("쓰기 테이블에 사용자 트리거가 있어 자동 등록할 수 없습니다.")
        columns = c.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
            a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid)
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped""", (relation,)).fetchall()
        types = {"set_id": "uuid", "type_slot": "smallint", "source_generated_id": "bigint", "content_json": "jsonb",
                 "created_at": "timestamp with time zone", "updated_at": "timestamp with time zone"}
        defaults = {"exam_format": "'pbt'::text", "created_at": "CURRENT_TIMESTAMP", "updated_at": "CURRENT_TIMESTAMP"}
        expected = [(field, types.get(field, "text"), True, "", "", defaults.get(field)) for field in fields]
        if sorted(columns) != sorted(expected):
            raise TransferError("쓰기 테이블의 컬럼·형식·기본값이 지원 구조와 다릅니다.")
        constraints = c.execute("""SELECT pg_get_constraintdef(oid),convalidated FROM pg_constraint
            WHERE conrelid=%s::regclass AND contype<>'n'""", (relation,)).fetchall()
        indexes = c.execute("""SELECT pg_get_indexdef(indexrelid),indisunique,indisvalid,indisready
            FROM pg_index WHERE indrelid=%s::regclass""", (relation,)).fetchall()
        if any(not valid for _, valid in constraints) or any(not valid or not ready for _, _, valid, ready in indexes):
            raise TransferError("쓰기 테이블의 제약·인덱스 검증 상태를 확인하세요.")
        result[table] = {
            "columns": sorted(list(row) for row in columns),
            "constraints": sorted(list(row) for row in constraints),
            "indexes": sorted([definition.split(" USING ", 1)[1], unique] for definition, unique, _, _ in indexes),
        }
    return result


def _rows(c, set_ids, source_keys=()):
    sets = [r[0] for r in c.execute("""SELECT to_jsonb(s) FROM topik_bank.writing_sets s
        WHERE set_id=ANY(%s::uuid[]) OR source_key=ANY(%s::text[]) ORDER BY set_id""", (set_ids, list(source_keys)))]
    questions = [r[0] for r in c.execute("""SELECT to_jsonb(q) FROM topik_bank.writing_questions q
        WHERE set_id=ANY(%s::uuid[]) ORDER BY set_id,type_slot""", ([r["set_id"] for r in sets],))]
    return {"writing_sets": sets, "writing_questions": questions}


def _validate(rows):
    groups = defaultdict(list)
    for question in rows["writing_questions"]:
        groups[question["set_id"]].append(question)
    for record in rows["writing_sets"]:
        if record["exam_format"] != "pbt":
            raise TransferError("현재는 PBT 쓰기 세트만 등록할 수 있습니다. IBT 문항 등록은 아직 지원하지 않습니다.")
        questions = groups[record["set_id"]]
        if [q["type_slot"] for q in questions] != [51, 52, 53, 54] or len({q["source_generated_id"] for q in questions}) != 4:
            raise TransferError("쓰기 세트에 51~54번이 중복 없이 각각 한 문항씩 필요합니다.")
        for row in questions:
            try:
                parsed = WritingQuestion.model_validate(row["content_json"])
            except ValueError:
                raise TransferError("쓰기 문항 형식이 올바르지 않습니다. Hub에서 수정한 뒤 다시 확인하세요.") from None
            if parsed.type_slot != row["type_slot"] or parsed.exam_format != record["exam_format"]:
                raise TransferError("쓰기 세트와 문항의 시험 형식·번호가 일치하지 않습니다.")


@dataclass
class WritingSnapshot:
    identity: dict
    schema: dict
    rows: dict
    set_ids: list[str]

    @property
    def signature(self):
        return digest((self.identity, self.schema, self.rows, self.set_ids))

    @property
    def source_keys(self):
        return [row["source_key"] for row in self.rows["writing_sets"]]


@dataclass
class WritingTransferPlan:
    snapshot: WritingSnapshot
    target_identity: dict
    target_signature: str
    create_tables: bool
    effects: dict


def list_source_sets(source_url):
    with connection(source_url) as c:
        if _schema(c) is None:
            raise TransferError("Hub에 쓰기 테이블이 없습니다. 쓰기 세트 저장을 먼저 준비하세요.")
        return [dict(zip(("set_id", "exam_format", "model", "created_at", "updated_at", "items"), row))
                for row in c.execute("""SELECT s.set_id::text,s.exam_format,s.model,s.created_at,s.updated_at,count(q.type_slot)
                    FROM topik_bank.writing_sets s LEFT JOIN topik_bank.writing_questions q USING(set_id)
                    GROUP BY s.set_id ORDER BY s.created_at DESC,s.set_id""")]


def capture_writing(source_url, set_ids):
    try:
        ids = sorted({str(uuid.UUID(str(value))) for value in set_ids})
    except (ValueError, TypeError, AttributeError):
        raise TransferError("세트 식별자를 확인하세요.") from None
    if not ids:
        raise TransferError("쓰기 세트를 하나 이상 선택하세요.")
    with connection(source_url) as c:
        schema = _schema(c)
        if schema is None:
            raise TransferError("Hub에 쓰기 테이블이 없습니다.")
        rows = _rows(c, ids)
        if len(rows["writing_sets"]) != len(ids):
            raise TransferError("선택한 쓰기 세트가 Hub에 없습니다. 목록을 새로고침하세요.")
        _validate(rows)
        return WritingSnapshot(database_identity(c), schema, rows, ids)


def _effects(source, target):
    by_id = {row["set_id"]: row for row in target["writing_sets"]}
    by_source = {row["source_key"]: row["set_id"] for row in target["writing_sets"]}
    result = {"created": 0, "updated": 0, "unchanged": 0}
    for row in source["writing_sets"]:
        old = by_id.get(row["set_id"])
        if (old and old["source_key"] != row["source_key"]) or by_source.get(row["source_key"], row["set_id"]) != row["set_id"]:
            raise TransferError("대상 쓰기 세트의 ID 또는 원본 식별자가 충돌합니다. 대상 데이터를 확인하세요.")
        current = [q for q in target["writing_questions"] if q["set_id"] == row["set_id"]]
        expected = [q for q in source["writing_questions"] if q["set_id"] == row["set_id"]]
        result["created" if old is None else "unchanged" if old == row and current == expected else "updated"] += 1
    return result


def prepare_writing_transfer(snapshot, target_url):
    with connection(target_url) as c:
        identity = _distinct_target(c, snapshot.identity)
        schema = _schema(c)
        if schema is not None and schema != snapshot.schema:
            raise TransferError("대상 쓰기 테이블의 제약·인덱스가 Hub와 다릅니다. 기존 구조를 확인하세요.")
        rows = _rows(c, snapshot.set_ids, snapshot.source_keys) if schema else {table: [] for table in FIELDS}
        return WritingTransferPlan(snapshot, identity, digest((schema, rows)), schema is None, _effects(snapshot.rows, rows))


def _backup(root, identity, schema, rows, set_ids):
    payload = {"format": "topik-writing-transfer-backup-v1", "target_fingerprint": digest(identity),
               "created_at": datetime.now(timezone.utc).isoformat(), "set_ids": set_ids,
               "schema": schema, "rows": rows, "content_hash": digest((schema, rows)),
               "create_tables_sql": SCHEMA_FILE.read_text(encoding="utf-8")}
    path = Path(root) / "backups" / "writing" / f"before_transfer_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError("backup mismatch")
    except (OSError, ValueError):
        raise TransferError("변경 전 쓰기 세트 백업에 실패했습니다. DB는 변경하지 않았습니다.") from None
    return path


def _upsert(c, table, rows):
    fields = FIELDS[table]
    names = sql.SQL(",").join(map(sql.Identifier, fields))
    keys = sql.SQL(",").join(map(sql.Identifier, KEYS[table]))
    updates = sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(field), sql.Identifier(field))
                              for field in fields if field not in KEYS[table])
    c.execute(sql.SQL("""INSERT INTO topik_bank.{table} ({names})
        SELECT {names} FROM jsonb_populate_recordset(NULL::topik_bank.{table},%s)
        ON CONFLICT ({keys}) DO UPDATE SET {updates}""").format(
            table=sql.Identifier(table), names=names, keys=keys, updates=updates), (Jsonb(rows),))


def _verify(c, snapshot):
    if _schema(c) != snapshot.schema or _rows(c, snapshot.set_ids, snapshot.source_keys) != snapshot.rows:
        raise TransferError("등록된 쓰기 세트의 구조 또는 내용이 Hub 원본과 일치하지 않습니다.")


def execute_writing_transfer(source_url, target_url, plan, root):
    fresh = capture_writing(source_url, plan.snapshot.set_ids)
    if fresh.signature != plan.snapshot.signature:
        raise TransferError("미리보기 이후 Hub 원본이 변경되었습니다. 등록 내용을 다시 확인하세요.")
    backup = None
    commit_attempted = committed = False
    try:
        with connection(target_url, readonly=False) as c:
            if _distinct_target(c, fresh.identity) != plan.target_identity:
                raise TransferError("대상 DB가 변경되었습니다. 등록 내용을 다시 확인하세요.")
            for lock in ("schema_migrations", "writing_transfer"):
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("topik_bank:" + lock,))
            schema = _schema(c)
            if schema:
                c.execute("LOCK TABLE topik_bank.writing_sets,topik_bank.writing_questions IN SHARE ROW EXCLUSIVE MODE")
                schema = _schema(c)
            previous = _rows(c, fresh.set_ids, fresh.source_keys) if schema else {table: [] for table in FIELDS}
            if digest((schema, previous)) != plan.target_signature:
                raise TransferError("미리보기 이후 대상 데이터가 변경되었습니다. 등록 내용을 다시 확인하세요.")
            effects = _effects(fresh.rows, previous)
            if schema is None or effects["created"] or effects["updated"]:
                backup = _backup(root, plan.target_identity, schema, previous, fresh.set_ids)
                if schema is None:
                    c.execute("CREATE SCHEMA IF NOT EXISTS topik_bank")
                    c.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
                if _schema(c) != fresh.schema:
                    raise TransferError("쓰기 테이블의 생성 구조가 Hub와 다릅니다. 변경을 롤백합니다.")
                for table in FIELDS:
                    old_rows = {tuple(row[key] for key in KEYS[table]): row for row in previous[table]}
                    changed = [row for row in fresh.rows[table] if old_rows.get(tuple(row[key] for key in KEYS[table])) != row]
                    if changed:
                        _upsert(c, table, changed)
            _verify(c, fresh)
            commit_attempted = True
        committed = True
        with connection(target_url) as c:
            if _distinct_target(c, fresh.identity) != plan.target_identity:
                raise TransferError("대상 DB가 변경되었습니다.")
            _verify(c, fresh)
        return {"status": "verified", **effects, "sets": len(fresh.set_ids),
                "questions": len(fresh.rows["writing_questions"]), "created_tables": schema is None,
                "backup": str(backup) if backup else None}
    except Exception as exc:
        if committed:
            message = "등록은 커밋되었지만 최종 조회 검증을 완료하지 못했습니다. 등록 내용을 다시 확인하세요."
        elif commit_attempted:
            message = "커밋 결과를 확정하지 못했습니다. 등록 내용을 다시 확인하세요."
        else:
            message = str(exc) if isinstance(exc, TransferError) else "쓰기 세트 등록에 실패해 변경을 롤백했습니다."
        if backup:
            message += f" 백업: {backup}"
        raise TransferError(message) from None
