"""Push a consistent authoring snapshot to a consumer bank; never write to the source."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


KEYS = {
    "question_sets": ("set_id",),
    "items": ("item_id",),
    "tts_audio_assets": ("audio_asset_id",),
    "item_audio_bindings": ("item_id", "content_hash", "audio_asset_id"),
    "question_set_item_audio_bindings": ("set_id", "position", "audio_asset_id"),
    "item_visual_assets": ("visual_asset_id",),
}
TABLES = tuple(KEYS)
VIEWS = ("current_items", "current_set_contents")
SCHEMA_FILE = Path(__file__).with_name("target_bank.sql")
NATURAL_KEYS = {
    "question_sets": ("section", "generator_provider", "generator_model", "generator_version", "set_sequence"),
    "items": ("source_key",),
    "tts_audio_assets": ("source_hash", "model_name", "female_voice", "male_voice"),
}


class TransferError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


@contextmanager
def connection(url, *, readonly=True):
    if not url.strip():
        raise TransferError("DB 연결 환경변수를 설정하세요.")
    try:
        with psycopg.connect(url, connect_timeout=8, prepare_threshold=None,
                             options="-c timezone=UTC -c statement_timeout=60000 -c lock_timeout=8000"
                             + (" -c default_transaction_read_only=on" if readonly else "")) as c:
            if readonly:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield c
    except psycopg.Error as exc:
        # Driver messages may include a DSN, row contents, or credentials.
        raise TransferError(f"DB 작업에 실패했습니다 (SQLSTATE {exc.sqlstate or '연결 오류'}). "
                            "연결·권한·스키마를 확인하고 등록 결과를 다시 확인하세요.") from None


def database_identity(c):
    identity = c.execute("SELECT current_database(),inet_server_addr()::text,inet_server_port()" ).fetchone()
    cluster = None
    if c.execute("SELECT has_function_privilege(current_user,'pg_control_system()','EXECUTE')").fetchone()[0]:
        cluster = c.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    return {"cluster": cluster, "database": identity[0], "address": identity[1], "port": identity[2]}


def _distinct_target(c, source_identity):
    identity = database_identity(c)
    same_cluster = identity["cluster"] and identity["cluster"] == source_identity["cluster"]
    loopbacks = {"127.0.0.1", "::1"}
    same_address = (identity["address"] == source_identity["address"] or
                    {identity["address"], source_identity["address"]}.issubset(loopbacks))
    same_endpoint = same_address and identity["port"] == source_identity["port"]
    if identity["database"] == source_identity["database"] and (same_cluster or same_endpoint):
        raise TransferError("대상이 생성기 원본 DB와 같습니다. 원본에는 전송할 수 없습니다.")
    return identity


def _normal(text):
    return re.sub(r"\s+", " ", text.replace('"', '')).strip()


def schema_description(c):
    names = list(TABLES + VIEWS)
    columns = c.execute("""SELECT cl.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
        FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace
        JOIN pg_attribute a ON a.attrelid=cl.oid
        WHERE n.nspname='topik_bank' AND cl.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY cl.relname,a.attname""", (names,)).fetchall()
    constraints = c.execute("""SELECT cl.relname,pg_get_constraintdef(co.oid)
        FROM pg_constraint co JOIN pg_class cl ON cl.oid=co.conrelid
        WHERE cl.relnamespace=to_regnamespace('topik_bank') AND cl.relname=ANY(%s)
        AND co.contype IN ('p','u','f','c')""", (list(TABLES),)).fetchall()
    indexes = c.execute("""SELECT cl.relname,pg_get_indexdef(i.indexrelid)
        FROM pg_index i JOIN pg_class cl ON cl.oid=i.indrelid
        WHERE cl.relnamespace=to_regnamespace('topik_bank') AND cl.relname=ANY(%s)
        AND i.indisvalid AND i.indisready""", (list(TABLES),)).fetchall()
    views = c.execute("""SELECT relname,pg_get_viewdef(oid) FROM pg_class
        WHERE relnamespace=to_regnamespace('topik_bank') AND relkind='v' AND relname=ANY(%s)""",
                      (list(VIEWS),)).fetchall()
    triggers = c.execute("""SELECT cl.relname FROM pg_trigger t JOIN pg_class cl ON cl.oid=t.tgrelid
        WHERE cl.relnamespace=to_regnamespace('topik_bank') AND cl.relname=ANY(%s)
        AND NOT t.tgisinternal AND t.tgenabled<>'D'""", (list(TABLES),)).fetchall()
    return {
        "columns": [list(r) for r in columns],
        "constraints": sorted([t, _normal(d)] for t, d in constraints),
        "indexes": sorted([t, _normal(d.split(" USING ", 1)[1]), "UNIQUE INDEX" in d] for t, d in indexes),
        "views": sorted([t, _normal(d)] for t, d in views),
        "triggers": sorted(r[0] for r in triggers),
    }


def schema_problems(expected, actual):
    problems = []
    for part in ("columns", "indexes", "views"):
        if expected[part] != actual[part]:
            problems.append({"columns": "컬럼·형식·NULL 조건", "indexes": "인덱스", "views": "조회 뷰"}[part])
    # Legacy Supabase metadata is retained; the target may accept this extra storage kind.
    def constraints(rows):
        return sorted([t, d.replace("'supabase'::text, ", "")] for t, d in rows)
    if constraints(expected["constraints"]) != constraints(actual["constraints"]):
        problems.append("기본 키·외래 키·고유키·CHECK 제약")
    for table, definition in expected["constraints"]:
        if "storage_backend" in definition:
            allowed = set(re.findall(r"'([a-z0-9_]+)'::text", definition))
            target = next((d for t, d in actual["constraints"] if t == table and "storage_backend" in d), "")
            if not allowed.issubset(set(re.findall(r"'([a-z0-9_]+)'::text", target))):
                problems.append(f"{table}: 원본 미디어 저장 방식 미지원")
    if actual["triggers"]:
        problems.append("지원하지 않는 사용자 트리거")
    return problems


def _read_rows(c, table, where="", params=()):
    query = sql.SQL("SELECT to_jsonb(t) FROM topik_bank.{} t " + where).format(sql.Identifier(table))
    return sorted((r[0] for r in c.execute(query, params)), key=lambda r: tuple(str(r[k]) for k in KEYS[table]))


def _all_rows(c):
    return {t: _read_rows(c, t) for t in TABLES}


def _selected_rows(c, set_ids):
    rows = {"question_sets": _read_rows(c, "question_sets", "WHERE set_id=ANY(%s::uuid[])", (set_ids,)),
            "items": _read_rows(c, "items", "WHERE set_id=ANY(%s::uuid[])", (set_ids,))}
    if len(rows["question_sets"]) != len(set_ids):
        raise TransferError("선택한 세트가 원본 DB에 없습니다. 목록을 새로 확인하세요.")
    item_ids = [r["item_id"] for r in rows["items"]]
    for t in ("item_audio_bindings", "item_visual_assets"):
        rows[t] = _read_rows(c, t, "WHERE item_id=ANY(%s::uuid[])", (item_ids,))
    rows["question_set_item_audio_bindings"] = _read_rows(
        c, "question_set_item_audio_bindings", "WHERE set_id=ANY(%s::uuid[])", (set_ids,))
    audio_ids = sorted({r["audio_asset_id"] for t in ("item_audio_bindings", "question_set_item_audio_bindings") for r in rows[t]})
    rows["tts_audio_assets"] = _read_rows(c, "tts_audio_assets", "WHERE audio_asset_id=ANY(%s::uuid[])", (audio_ids,))
    return rows


@dataclass
class Snapshot:
    source_identity: dict
    schema: dict
    rows: dict
    set_ids: list[str] | None

    @property
    def signature(self):
        return digest((self.source_identity, self.schema, self.rows, self.set_ids))

    @property
    def counts(self):
        return {t: len(self.rows[t]) for t in TABLES}


def capture_source(source_url, set_ids=None):
    if set_ids is not None:
        set_ids = sorted({str(uuid.UUID(str(s))) for s in set_ids})
        if not set_ids:
            raise TransferError("세트를 하나 이상 선택하세요.")
    with connection(source_url) as c:
        schema = schema_description(c)
        present = {r[0] for r in schema["columns"]}
        if not set(TABLES + VIEWS).issubset(present):
            raise TransferError("원본 문제은행의 6개 테이블·2개 뷰가 준비되지 않았습니다.")
        rows = _all_rows(c) if set_ids is None else _selected_rows(c, set_ids)
        return Snapshot(database_identity(c), schema, rows, set_ids)


def list_source_sets(source_url):
    with connection(source_url) as c:
        return [dict(zip(("set_id", "section", "model", "sequence", "status", "items"), r)) for r in c.execute("""
            SELECT s.set_id::text,s.section,s.generator_version,s.set_sequence,s.review_status,count(i.item_id)
            FROM topik_bank.question_sets s LEFT JOIN topik_bank.items i ON i.set_id=s.set_id
            GROUP BY s.set_id ORDER BY s.section,s.generator_version,s.set_sequence""")]


def initialize_target(source_url, target_url):
    with connection(source_url) as c:
        source_identity, expected = database_identity(c), schema_description(c)
    with connection(target_url, readonly=False) as c:
        _distinct_target(c, source_identity)
        c.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:bank_transfer_schema'))")
        actual = schema_description(c)
        if actual["columns"]:
            issues = schema_problems(expected, actual)
            if issues:
                raise TransferError("기존 대상 구조가 다릅니다: " + ", ".join(issues) + ". 자동 변경하지 않았습니다.")
            return False
        c.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        issues = schema_problems(expected, schema_description(c))
        if issues:
            raise TransferError("원본이 지원 스키마와 다릅니다: " + ", ".join(issues) + ". 초기화를 롤백했습니다.")
    return True


def _ids(rows, table):
    return {r[KEYS[table][0]] for r in rows[table]}


def transfer_effects(source, target):
    effects = {"replace": {}, "scope": {}, "detached": [], "blockers": []}
    for table, fields in NATURAL_KEYS.items():
        key = KEYS[table][0]
        ids = _ids(source, table)
        identities = {tuple(r[f] for f in fields) for r in source[table]}
        collisions = {r[key] for r in target[table] if tuple(r[f] for f in fields) in identities}
        effects["replace"][table] = sorted(collisions - ids)
        effects["scope"][table] = sorted(ids | collisions)
    sets, items = set(effects["scope"]["question_sets"]), set(effects["scope"]["items"])
    for r in target["items"]:
        if r["item_id"] in items and r["set_id"] and r["set_id"] not in sets:
            effects["blockers"].append("다른 대상 세트에 속한 문항: " + r["set_id"] + " (해당 세트도 선택하세요)")
        if r["set_id"] in sets and r["item_id"] not in items:
            effects["detached"].append(r["item_id"])
    visual_ids = _ids(source, "item_visual_assets")
    if any(r["visual_asset_id"] in visual_ids and r["item_id"] not in items for r in target["item_visual_assets"]):
        effects["blockers"].append("다른 문항이 같은 이미지 ID를 사용하고 있습니다.")
    source_audio = {r["audio_asset_id"]: r for r in source["tts_audio_assets"]}
    changed_audio = set(effects["replace"]["tts_audio_assets"]) | {
        r["audio_asset_id"] for r in target["tts_audio_assets"]
        if r["audio_asset_id"] in source_audio and r != source_audio[r["audio_asset_id"]]}
    for table, owner, scope in (("item_audio_bindings", "item_id", items),
                                ("question_set_item_audio_bindings", "set_id", sets)):
        if any(r["audio_asset_id"] in changed_audio and r[owner] not in scope for r in target[table]):
            effects["blockers"].append("선택 범위 밖에서도 사용하는 음원이 변경됩니다. 관련 세트도 선택하세요.")
    effects["detached"].sort()
    effects["blockers"] = sorted(set(effects["blockers"]))
    return effects


def _external_references(c):
    return [(r[0], r[1]) for r in c.execute("""SELECT parent.relname,child.relname
        FROM pg_constraint co JOIN pg_class parent ON parent.oid=co.confrelid
        JOIN pg_class child ON child.oid=co.conrelid
        WHERE co.contype='f' AND parent.relnamespace=to_regnamespace('topik_bank')
        AND parent.relname=ANY(%s)
        AND NOT (child.relnamespace=to_regnamespace('topik_bank') AND child.relname=ANY(%s))""",
                                                          (list(TABLES), list(TABLES)))]


def _effect_blockers(c, rows, target, effects):
    deletes = {t for t, ids in effects["replace"].items() if ids}
    for table, owner, scope in (("item_audio_bindings", "item_id", effects["scope"]["items"]),
                                ("question_set_item_audio_bindings", "set_id", effects["scope"]["question_sets"])):
        if any(r[owner] in scope for r in target[table]):
            deletes.add(table)
    source_visual_ids = _ids(rows, "item_visual_assets")
    if any(r["item_id"] in effects["scope"]["items"] and
           (r["visual_asset_id"] not in source_visual_ids or r["item_id"] in effects["replace"]["items"])
           for r in target["item_visual_assets"]):
        deletes.add("item_visual_assets")
    for table, _ in _external_references(c):
        if table in deletes:
            effects["blockers"].append(f"{table}: ID 교체·연결 정리 범위에 외부 테이블 참조가 있습니다.")


def _projection(rows, desired):
    sets, items = _ids(desired, "question_sets"), _ids(desired, "items")
    return {t: [r for r in rows[t] if (
        (r["item_id"] in items or r["set_id"] in sets) if t == "items" else
        r["item_id"] in items if t in ("item_audio_bindings", "item_visual_assets") else
        r["set_id"] in sets if t == "question_set_item_audio_bindings" else
        r[KEYS[t][0]] in _ids(desired, t))] for t in TABLES}


@dataclass
class TransferPlan:
    snapshot: Snapshot
    target_identity: dict
    target_signature: str
    effects: dict
    already_matches: bool


def prepare_transfer(snapshot, target_url):
    with connection(target_url) as c:
        identity = _distinct_target(c, snapshot.source_identity)
        actual = schema_description(c)
        issues = schema_problems(snapshot.schema, actual)
        if issues:
            raise TransferError("대상 DB 구조 확인 필요: " + ", ".join(issues) + ". 빈 DB는 먼저 초기화하세요.")
        target = _all_rows(c)
        effects = transfer_effects(snapshot.rows, target)
        matches = _projection(target, snapshot.rows) == snapshot.rows and not any(effects["replace"].values())
        if not matches:
            _effect_blockers(c, snapshot.rows, target, effects)
        return TransferPlan(snapshot, identity, digest((actual, target)), effects, matches)


def _delete_ids(c, table, ids):
    if ids:
        c.execute(sql.SQL("DELETE FROM topik_bank.{} WHERE {}=ANY(%s::uuid[])").format(
            sql.Identifier(table), sql.Identifier(KEYS[table][0])), (list(ids),))


def _upsert(c, table, rows):
    if not rows:
        return
    fields = sorted(rows[0])
    names = sql.SQL(",").join(map(sql.Identifier, fields))
    updates = sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(k), sql.Identifier(k))
                              for k in fields if k not in KEYS[table])
    c.execute(sql.SQL("""INSERT INTO topik_bank.{table} ({columns})
        SELECT {columns} FROM jsonb_populate_recordset(NULL::topik_bank.{table},%s)
        ON CONFLICT ({keys}) DO UPDATE SET {updates}""").format(
            table=sql.Identifier(table), columns=names,
            keys=sql.SQL(",").join(map(sql.Identifier, KEYS[table])), updates=updates), (Jsonb(rows),))


def _apply(c, rows, effects):
    sets, items = effects["scope"]["question_sets"], effects["scope"]["items"]
    c.execute("DELETE FROM topik_bank.question_set_item_audio_bindings WHERE set_id=ANY(%s::uuid[])", (sets,))
    c.execute("DELETE FROM topik_bank.item_audio_bindings WHERE item_id=ANY(%s::uuid[])", (items,))
    c.execute("""DELETE FROM topik_bank.item_visual_assets WHERE item_id=ANY(%s::uuid[])
        AND (NOT (visual_asset_id=ANY(%s::uuid[])) OR item_id=ANY(%s::uuid[]))""",
              (items, sorted(_ids(rows, "item_visual_assets")), effects["replace"]["items"]))
    c.execute("UPDATE topik_bank.item_visual_assets SET is_current=false WHERE item_id=ANY(%s::uuid[])", (items,))
    c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE set_id=ANY(%s::uuid[]) OR item_id=ANY(%s::uuid[])", (sets, items))
    _delete_ids(c, "items", effects["replace"]["items"])
    _delete_ids(c, "question_sets", effects["replace"]["question_sets"])
    _delete_ids(c, "tts_audio_assets", effects["replace"]["tts_audio_assets"])
    # Vacate unique values inside the transaction to allow identity/position swaps.
    for table, field in (("question_sets", "generator_version"), ("items", "source_key"), ("tts_audio_assets", "source_hash")):
        key = KEYS[table][0]
        for row in rows[table]:
            c.execute(sql.SQL("UPDATE topik_bank.{} SET {}=%s WHERE {}=%s").format(
                sql.Identifier(table), sql.Identifier(field), sql.Identifier(key)),
                (digest((uuid.uuid4().hex, row[key])), row[key]))
    for table in TABLES:
        _upsert(c, table, rows[table])


def execute_transfer(source_url, target_url, plan):
    fresh = capture_source(source_url, plan.snapshot.set_ids)
    if fresh.signature != plan.snapshot.signature:
        raise TransferError("미리보기 이후 원본이 변경되었습니다. 다시 확인하세요.")
    committed = False
    try:
        with connection(target_url, readonly=False) as c:
            identity = _distinct_target(c, fresh.source_identity)
            if identity != plan.target_identity:
                raise TransferError("대상 DB가 변경되었습니다. 다시 확인하세요.")
            c.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                sql.SQL(",").join(sql.Identifier("topik_bank", t) for t in TABLES)))
            actual, target = schema_description(c), _all_rows(c)
            issues = schema_problems(fresh.schema, actual)
            if issues:
                raise TransferError("대상 구조가 변경되었습니다: " + ", ".join(issues))
            effects = transfer_effects(fresh.rows, target)
            if _projection(target, fresh.rows) == fresh.rows and not any(effects["replace"].values()):
                return {"status": "verified", "counts": fresh.counts, "detached": 0}
            if digest((actual, target)) != plan.target_signature:
                raise TransferError("미리보기 이후 대상 데이터가 변경되었습니다. 다시 확인하세요.")
            _effect_blockers(c, fresh.rows, target, effects)
            if effects["blockers"]:
                raise TransferError(" / ".join(effects["blockers"]))
            _apply(c, fresh.rows, effects)
            if _projection(_all_rows(c), fresh.rows) != fresh.rows:
                raise TransferError("복사 결과가 원본과 달라 전체 롤백했습니다.")
        committed = True
        with connection(target_url) as c:
            if _projection(_all_rows(c), fresh.rows) != fresh.rows:
                raise TransferError("등록 후 대상 내용이 변경되었습니다. 다시 확인하세요.")
        return {"status": "verified", "counts": fresh.counts, "detached": len(effects["detached"])}
    except TransferError as exc:
        if committed:
            raise TransferError("DB 커밋 후 재조회 검증을 완료하지 못했습니다. 같은 선택으로 다시 확인하세요.") from None
        raise exc


def check_shared_media(snapshot, cloudfront_base_url):
    """Check current shared URLs with HEAD; archived/local metadata is still copied verbatim."""
    from concurrent.futures import ThreadPoolExecutor
    import httpx
    from topik_question_lab.step_06_publication.media_cloud import cloudfront_url, content_type

    items = {r["item_id"]: r for r in snapshot.rows["items"]}
    audio_ids = {r["audio_asset_id"] for r in snapshot.rows["item_audio_bindings"]
                 if r["is_current"] and r["content_hash"] == items[r["item_id"]]["content_hash"]}
    audio_ids.update(r["audio_asset_id"] for r in snapshot.rows["question_set_item_audio_bindings"] if r["is_current"])
    active = [r for r in snapshot.rows["tts_audio_assets"] if r["audio_asset_id"] in audio_ids and not r["deleted_at"]]
    active += [r for r in snapshot.rows["item_visual_assets"] if r["is_current"] and r["content_hash"] == items[r["item_id"]]["content_hash"]]
    files = {(r["storage_url"], r["byte_size"], r["mime_type"]): r for r in active}
    report = {"checked": 0, "failed": 0, "unverified": 0, "legacy_or_local": 0}
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        def check(row):
            if row["storage_backend"] != "s3":
                return "legacy_or_local"
            if not cloudfront_base_url:
                return "unverified"
            try:
                # Only contact the configured shared CloudFront origin.
                if cloudfront_url(cloudfront_base_url, row["storage_path"]) != row["storage_url"]:
                    return "failed"
                response = client.head(row["storage_url"], headers={"Accept-Encoding": "identity"})
                if (response.status_code != 200 or
                        int(response.headers.get("content-length", -1)) != row["byte_size"] or
                        content_type(response.headers.get("content-type", "")) != row["mime_type"]):
                    return "failed"
                return "checked"
            except (ValueError, httpx.HTTPError):
                return "failed"
        with ThreadPoolExecutor(max_workers=8) as pool:
            for result in pool.map(check, files.values()):
                report[result] += 1
    return report
