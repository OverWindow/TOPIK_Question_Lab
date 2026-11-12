"""Preview and import dictionary senses into existing Django-managed tables."""
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
from itertools import islice
import json
from pathlib import Path
import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from topik_question_lab.common.question_bank.connection import TransferError, connection, database_identity, digest
from topik_question_lab.menus.korean_dictionary.korean_dictionary_source import file_fingerprints


TABLE = "public.korean_dictionary_senses"
FIELDS = ("headword", "homonym_number", "part_of_speech", "entry_type", "sense_order",
          "definition", "translations", "examples", "metadata", "source_file", "source_row")
BATCH_SIZE = 500


def _schema(c):
    relation = c.execute("""SELECT relkind,relrowsecurity,relforcerowsecurity FROM pg_class
        WHERE oid=to_regclass(%s)""", (TABLE,)).fetchone()
    if relation is None:
        raise TransferError("사전 테이블이 없습니다. unigate-main의 vocabulary.0002_korean_dictionary_senses "
                            "마이그레이션을 대상 DB에 먼저 적용하세요.")
    if relation != ("r", False, False):
        raise TransferError("사전 등록에는 행 보안이 없는 일반 테이블이 필요합니다.")
    if c.execute("""SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=%s::regclass
        AND NOT tgisinternal AND tgenabled<>'D')""", (TABLE,)).fetchone()[0]:
        raise TransferError("사전 테이블에 사용자 트리거가 있어 등록을 중단합니다.")
    columns = c.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
        a.attidentity,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attname""", (TABLE,)).fetchall()
    expected = dict(zip(("id",) + FIELDS, (
        "bigint", "text", "smallint", "character varying(30)", "character varying(16)",
        "integer", "text", "jsonb", "text", "jsonb", "character varying(255)", "integer")))
    if {r[0]: r[1] for r in columns} != expected or not all(r[2] for r in columns):
        raise TransferError("사전 컬럼·형식·NULL 조건이 unigate-main의 사전 구조와 다릅니다.")
    id_column = next(r for r in columns if r[0] == "id")
    if not id_column[3] and not (id_column[4] or "").startswith("nextval("):
        raise TransferError("사전 ID 자동 증가 설정을 확인하세요.")
    constraints = c.execute("""SELECT contype,condeferrable,convalidated,pg_get_constraintdef(oid)
        FROM pg_constraint WHERE conrelid=%s::regclass AND contype<>'n' ORDER BY 1,4""", (TABLE,)).fetchall()
    if ("p", False, True, "PRIMARY KEY (id)") not in constraints or (
            "u", False, True, "UNIQUE (source_file, source_row)") not in constraints:
        raise TransferError("사전 ID 기본키 또는 원본 파일·행 고유 제약을 확인하세요.")
    indexes = c.execute("""SELECT pg_get_indexdef(indexrelid),indisvalid,indisready FROM pg_index
        WHERE indrelid=%s::regclass ORDER BY 1""", (TABLE,)).fetchall()
    return {"columns": columns, "constraints": constraints, "indexes": indexes}


def _target_state(c, snapshot, schema, backup=None):
    content_hash, ids_hash = hashlib.sha256(), hashlib.sha256()
    count, updated, unchanged, unexpected = 0, 0, 0, False
    with c.cursor(name="dictionary_" + uuid.uuid4().hex) as cursor:
        cursor.itersize = 1000
        cursor.execute(sql.SQL("SELECT {} FROM public.korean_dictionary_senses ORDER BY source_file,source_row")
                       .format(sql.SQL(",").join(map(sql.Identifier, ("id",) + FIELDS))))
        for values in cursor:
            record = dict(zip(("id",) + FIELDS, values))
            content_hash.update(digest(record).encode())
            key = (record["source_file"], record["source_row"])
            ids_hash.update(digest((key, record["id"])).encode())
            if backup is not None:
                backup.write(json.dumps(record, ensure_ascii=False) + "\n")
            record.pop("id")
            expected = snapshot.hashes.get(key)
            unexpected |= expected is None
            same = expected == digest(record)
            unchanged += same
            updated += not same
            count += 1
    if count and (unexpected or count != len(snapshot.hashes)):
        raise TransferError("기존 사전의 원본 파일명·행 번호가 이번 자료와 다릅니다. "
                            "같은 전체 원본을 준비하세요. 일부 파일 등록이나 다른 배포본 교체는 지원하지 않습니다.")
    return {"signature": digest((schema, content_hash.hexdigest())), "ids": ids_hash.hexdigest(),
            "count": count, "created": len(snapshot.hashes) if not count else 0,
            "updated": updated, "unchanged": unchanged}


@dataclass
class DictionaryPlan:
    identity: dict
    schema: dict
    state: dict
    source_signature: str


def prepare_dictionary_import(snapshot, target_url):
    with connection(target_url) as c:
        schema = _schema(c)
        return DictionaryPlan(database_identity(c), schema, _target_state(c, snapshot, schema),
                              digest(snapshot.fingerprints))


def _backup(c, snapshot, plan, root):
    folder = Path(root) / "backups" / "korean_dictionary"
    path = folder / f"before_import_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}.jsonl.gz"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "xt", encoding="utf-8") as handle:
            handle.write(json.dumps({"format": "korean-dictionary-backup-v1", "table": TABLE,
                                     "target_fingerprint": digest(plan.identity), "schema": plan.schema,
                                     "signature": plan.state["signature"]}, ensure_ascii=False) + "\n")
            state = _target_state(c, snapshot, plan.schema, backup=handle)
        checksum = hashlib.sha256()
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            header = json.loads(next(handle))
            for line in handle:
                checksum.update(digest(json.loads(line)).encode())
        if state["signature"] != plan.state["signature"] or header["signature"] != digest((plan.schema, checksum.hexdigest())):
            raise ValueError("backup mismatch")
    except Exception:
        raise TransferError("변경 전 사전 백업에 실패했습니다. 등록을 중단했습니다.") from None
    return path


def _write_batch(c, records, existing):
    names = sql.SQL(",").join(map(sql.Identifier, FIELDS))
    if existing:
        updates = FIELDS[:-2]
        target_values = sql.SQL(",").join(sql.SQL("t.{}").format(sql.Identifier(n)) for n in updates)
        source_values = sql.SQL(",").join(sql.SQL("s.{}").format(sql.Identifier(n)) for n in updates)
        assignments = sql.SQL(",").join(sql.SQL("{}=s.{}").format(sql.Identifier(n), sql.Identifier(n)) for n in updates)
        c.execute(sql.SQL("""UPDATE public.korean_dictionary_senses t SET {assignments}
            FROM jsonb_populate_recordset(NULL::public.korean_dictionary_senses,%s) s
            WHERE t.source_file=s.source_file AND t.source_row=s.source_row
            AND ROW({target_values}) IS DISTINCT FROM ROW({source_values})""").format(
                assignments=assignments, target_values=target_values, source_values=source_values), (Jsonb(records),))
    else:
        c.execute(sql.SQL("""INSERT INTO public.korean_dictionary_senses ({names})
            SELECT {names} FROM jsonb_populate_recordset(NULL::public.korean_dictionary_senses,%s)""")
                  .format(names=names), (Jsonb(records),))


def execute_dictionary_import(snapshot, target_url, plan, root, progress=None):
    backup, commit_attempted, committed = None, False, False
    try:
        if digest(snapshot.fingerprints) != plan.source_signature or file_fingerprints(snapshot.directory) != snapshot.fingerprints:
            raise TransferError("미리보기 이후 원본 파일이 변경되었습니다. 등록 내용을 다시 확인하세요.")
        with connection(target_url, readonly=False) as c:
            if database_identity(c) != plan.identity:
                raise TransferError("대상 DB가 변경되었습니다. 등록 내용을 다시 확인하세요.")
            _schema(c)
            c.execute("LOCK TABLE public.korean_dictionary_senses IN SHARE ROW EXCLUSIVE MODE")
            schema = _schema(c)
            before = _target_state(c, snapshot, schema)
            if before["signature"] != plan.state["signature"]:
                raise TransferError("미리보기 이후 대상 사전이 변경되었습니다. 등록 내용을 다시 확인하세요.")
            if before["updated"]:
                if progress:
                    progress("기존 사전 백업 중")
                backup = _backup(c, snapshot, plan, root)
            if before["created"] or before["updated"]:
                records, transferred = snapshot.records(), 0
                while batch := list(islice(records, BATCH_SIZE)):
                    _write_batch(c, batch, existing=bool(before["count"]))
                    transferred += len(batch)
                    if progress:
                        progress(f"{transferred:,}/{len(snapshot.hashes):,}행 처리 중 · 전체 성공 후 확정")
            after = _target_state(c, snapshot, schema)
            if after["unchanged"] != len(snapshot.hashes) or (before["count"] and before["ids"] != after["ids"]):
                raise TransferError("등록 내용 또는 기존 ID 보존 검증에 실패했습니다.")
            commit_attempted = True
        committed = True
        if progress:
            progress("등록 후 DB 재조회 검증 중")
        with connection(target_url) as c:
            if database_identity(c) != plan.identity or _schema(c) != schema:
                raise TransferError("대상 DB 또는 테이블 구조가 변경되었습니다.")
            if _target_state(c, snapshot, schema)["signature"] != after["signature"]:
                raise TransferError("커밋 후 전체 내용 검증에 실패했습니다.")
        return {"status": "verified", "created": before["created"], "updated": before["updated"],
                "unchanged": before["unchanged"], "total": len(snapshot.hashes),
                "backup": str(backup) if backup else None}
    except Exception as exc:
        if committed:
            message = "등록은 커밋되었지만 최종 조회 검증을 완료하지 못했습니다. 등록 내용을 다시 확인하세요."
        elif commit_attempted:
            message = "커밋 결과를 확정하지 못했습니다. 등록 내용을 다시 확인하세요."
        else:
            message = str(exc) if isinstance(exc, TransferError) else "사전 등록에 실패했습니다. 연결·파일·권한을 확인하세요."
            message += " 사전 데이터 변경은 커밋되지 않았습니다."
        if backup:
            message += f" 백업: {backup}"
        raise TransferError(message) from None
