"""Durable SQLite-batch registration into the local PostgreSQL question bank."""
from __future__ import annotations

from topik_question_lab.common.generation.exam_profiles import profile_for, question_count
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from topik_question_lab.common.generation.generation_service import _check_structure
from topik_question_lab.common.listening_models import GeneratedListeningQuestion
from topik_question_lab.common.generation.listening_profiles import LISTENING_TYPE_PROFILES
from topik_question_lab.common.generation.listening_validation import validate_listening_question
from topik_question_lab.common.models import GeneratedQuestion
from topik_question_lab.common.generation.prompt_profiles import QUESTION_TYPE_PROFILES
from topik_question_lab.common.question_bank.publication import PUBLICATION_NAMESPACE, PublicationCandidate, PublicationItemMetadata, build_set_draft, collect_candidate_catalog
from topik_question_lab.common.question_bank.postgres_inspection import database_fingerprint, _safe_error as _database_error
from topik_question_lab.common.question_bank.postgres_storage import _psycopg, _uses_unversioned_schema, _upsert_item, ITEM_COLUMNS, _insert_set_items, _source_provenance
from topik_question_lab.common.generation.validation import validate_question


class PublicationBlocked(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _target_fingerprint(url):
    if not url:
        return ""
    if url.startswith(("postgres://", "postgresql://")):
        return database_fingerprint(url)
    from psycopg.conninfo import conninfo_to_dict
    config = conninfo_to_dict(url)
    return _hash({k: config.get(k, "") for k in ("host", "hostaddr", "port", "dbname", "user", "service")})


def _safe_error(exc, url):
    message = _database_error(exc, url)
    if url and not url.startswith(("postgres://", "postgresql://")):
        from psycopg.conninfo import conninfo_to_dict
        try:
            password = conninfo_to_dict(url).get("password")
            if password:
                message = message.replace(password, "***")
        except Exception:
            pass
    return message


def batch_set_id(section, batch_id):
    return str(uuid.uuid5(PUBLICATION_NAMESPACE, f"batch-set:{section}:{batch_id}"))


def read_batches(root: Path):
    path = root / "data/bulk_generation.db"
    if not path.exists():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM batches ORDER BY created_at DESC,rowid DESC")]
    except sqlite3.DatabaseError as exc:
        raise PublicationBlocked("생성 묶음 DB를 읽을 수 없습니다.") from exc


def validate_batch(batch, records, *, require_approval=True):
    if batch["status"] != "succeeded":
        raise PublicationBlocked("생성이 완료되지 않았습니다.")
    expected_count = question_count(batch.get("exam_format", "pbt"))
    if len(records) != expected_count:
        raise PublicationBlocked(f"실제 문항 {len(records)}/{expected_count}개입니다. 누락·중복을 확인하세요.")
    groups = defaultdict(list)
    for record in records:
        candidate = record.candidate
        if require_approval and not record.eligible:
            raise PublicationBlocked(f"{candidate.slot}번: {record.reason}")
        if any(issue.get("severity", "error") == "error" for issue in candidate.validation):
            raise PublicationBlocked(f"{candidate.slot}번: 자동 검사 오류가 남아 있습니다.")
        if (candidate.section, candidate.provider_key, candidate.model_id) != (
            batch["section"], batch["provider"], batch["model"]
        ):
            raise PublicationBlocked("생성 묶음의 영역·모델과 문항이 다릅니다.")
        if candidate.exam_format != batch.get("exam_format", "pbt"):
            raise PublicationBlocked("생성 묶음과 문항의 시험 형식이 다릅니다.")
        reading = candidate.section == "reading"
        model = GeneratedQuestion if reading else GeneratedListeningQuestion
        question = model.model_validate(candidate.question)
        if question.question_type != candidate.question_type:
            raise PublicationBlocked(f"{candidate.slot}번: 문항 유형이 저장 위치와 다릅니다.")
        issues = (validate_question if reading else validate_listening_question)(question, [])
        errors = [issue.message for issue in issues if issue.severity == "error"]
        if errors:
            raise PublicationBlocked(f"{candidate.slot}번: {'; '.join(errors)}")
        groups[candidate.question_type].append(question)
    profiles = QUESTION_TYPE_PROFILES if batch["section"] == "reading" else LISTENING_TYPE_PROFILES
    for type_id, questions in groups.items():
        profile = profile_for(batch["section"], type_id, questions[0].generation_profile)
        reading = batch["section"] == "reading"
        _check_structure(questions, profile, len(profile.question_numbers),
                         profile.shared_passage if reading else profile.shared_script, reading)


def make_package(batch, records, metadata=None, *, require_approval=True):
    validate_batch(batch, records, require_approval=require_approval)
    metadata = metadata or {}
    candidates = sorted((r.candidate for r in records), key=lambda c: c.slot)
    overrides = {key: PublicationItemMetadata(**value) for key, value in metadata.get("items", {}).items()}
    draft = build_set_draft(candidates, overrides, int(metadata.get("level", 4)), float(metadata.get("difficulty", 0.0)))
    return {
        "batch_id": batch["id"], "set_id": batch_set_id(batch["section"], batch["id"]),
        "metadata": metadata, "source_hash": _hash([asdict(c) for c in candidates]),
        "candidates": [asdict(c) for c in candidates],
        "item_metadata": {i.candidate.source_key: asdict(i.metadata) for i in draft.items},
        "level": draft.default_target_level, "difficulty": draft.default_predicted_difficulty,
    }


def package_draft(package):
    return build_set_draft(
        [PublicationCandidate(**c) for c in package["candidates"]],
        {key: PublicationItemMetadata(**value) for key, value in package["item_metadata"].items()},
        package["level"], package["difficulty"],
    )


class PublicationJobs:
    def __init__(self, root, *, readonly=False):
        self.path = Path(root) / "data/publication_jobs.db"
        self.readonly = readonly
        if readonly:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, target TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

    def connect(self):
        c = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15) if self.readonly else sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        return closing_transaction(c)

    def save(self, target, packages):
        payload = {"version": 1, "target": target, "packages": packages}
        plan_id = _hash(payload)
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO jobs(id,target,payload,status) VALUES (?,?,?,'prepared')",
                      (plan_id, target, _json(payload)))
        return self.get(plan_id)

    def get(self, plan_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise PublicationBlocked("등록 작업을 찾을 수 없습니다.")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"])
        if _hash(result["payload"]) != plan_id:
            raise PublicationBlocked("저장된 등록 작업의 해시가 다릅니다.")
        return result

    def update(self, plan_id, status, result=None, message=""):
        with self.connect() as c:
            c.execute("UPDATE jobs SET status=?,result=?,message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (status, _json(result or {}), message, plan_id))

    def list(self):
        if self.readonly and not self.path.exists():
            return []
        with self.connect() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM jobs ORDER BY created_at DESC,rowid DESC")]
        return [self.get(value) for value in ids]


class closing_transaction:
    """sqlite3's built-in context manager commits but does not close."""
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection.__enter__()

    def __exit__(self, *args):
        try:
            return self.connection.__exit__(*args)
        finally:
            self.connection.close()


def _dicts(cursor):
    names = [c.name for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def require_target_schema(c):
    if not _uses_unversioned_schema(c):
        raise PublicationBlocked("로컬 PostgreSQL에 문항 버전 제거 마이그레이션(006)이 필요합니다. DB 스키마는 변경하지 않았습니다.")
    format_columns = c.execute("""SELECT table_name FROM information_schema.columns
        WHERE table_schema='topik_bank' AND table_name IN ('items','question_sets') AND column_name='exam_format'""").fetchall()
    if len(format_columns) != 2:
        raise PublicationBlocked("PostgreSQL 시험 형식 마이그레이션(010)을 적용해야 합니다. DB 스키마는 변경하지 않았습니다.")
    columns = {
        "items": ",".join(ITEM_COLUMNS),
        "question_sets": "exam_format,set_id,section,generator_provider,generator_model,generator_version,set_sequence,review_status,default_target_level,published_at",
        "current_set_contents": "set_id,position,item_id,content_json",
    }
    for table, names in columns.items():
        c.execute(f"SELECT {names} FROM topik_bank.{table} LIMIT 0")
    constraints = _dicts(c.execute("""SELECT conrelid::regclass::text AS table_name,contype,
        pg_get_constraintdef(oid) AS definition FROM pg_constraint
        WHERE connamespace='topik_bank'::regnamespace"""))
    for table, fragment in (
        ("items", "PRIMARY KEY (item_id)"), ("items", "UNIQUE (source_key)"),
        ("items", "UNIQUE (set_id, position)"),
        ("items", "FOREIGN KEY (set_id)"),
        ("items", "FOREIGN KEY (set_id, exam_format)"),
        ("items", "CHECK (((set_id IS NULL) = (position IS NULL)))"),
        ("question_sets", "PRIMARY KEY (set_id)"),
        ("question_sets", "UNIQUE (exam_format, section, generator_provider, generator_model, generator_version, set_sequence)"),
    ):
        if not any(r["table_name"] == f"topik_bank.{table}" and fragment in r["definition"].replace('"', '') for r in constraints):
            raise PublicationBlocked(f"로컬 PostgreSQL 필수 제약이 없습니다: {table} {fragment}")


def expected_item(item, review_status="reviewed"):
    c = item.candidate
    return {
        "exam_format": c.exam_format, "section": c.section, "type_slot": c.slot, "item_type": c.question_type,
        "primary_skill": item.metadata.primary_skill.strip(), "target_level": item.metadata.target_level,
        "stem_length": item.stem_length, "choice_count": item.choice_count,
        "generator_provider": c.backend, "generator_model": c.provider_key, "generator_version": c.model_id,
        "prompt_version": c.prompt_version, "review_status": review_status, "stem": item.stem,
        "choices": item.choices, "correct_answer": c.question["answer"],
        "explanation": c.question.get("explanation", ""), "content_json": c.question,
        "content_hash": item.content_hash,
    }


def check_registered_set(c, package, *, allow_pending=False):
    """Return complete or absent; any semantic overlap mismatch is a conflict."""
    draft = package_draft(package)
    sources = {i.candidate.source_key: i for i in draft.items}
    rows = _dicts(c.execute("""SELECT * FROM topik_bank.items WHERE source_key=ANY(%s)""", (list(sources),)))
    item_ids = {}
    review_status = package.get("review_status", "reviewed")
    pending = False
    for row in rows:
        item = sources[row["source_key"]]
        if str(row["item_id"]) != str(item.candidate.item_id):
            raise PublicationBlocked(f"로컬 PostgreSQL 문항 ID 충돌: {item.candidate.source_key}")
        status = row["review_status"]
        if allow_pending and status == "pending_review" and review_status == "reviewed":
            pending = True
            status = review_status
        if any((status if key == "review_status" else row[key]) != value
               for key, value in expected_item(item, review_status).items()):
            raise PublicationBlocked(f"로컬 PostgreSQL 문항 내용 충돌: {item.candidate.source_key}")
        provenance = row["source_provenance"] or {}
        if any(provenance.get(k) != v for k, v in _source_provenance(item.candidate, item.metadata).items()):
            raise PublicationBlocked(f"로컬 PostgreSQL 문항 출처 충돌: {item.candidate.source_key}")
        item_ids[item.candidate.slot] = row["item_id"]
    memberships = _dicts(c.execute("""SELECT set_id,position,source_key FROM topik_bank.items
        WHERE set_id IS NOT NULL AND source_key=ANY(%s)""", (list(sources),)))
    set_ids = {str(r["set_id"]) for r in memberships}
    set_ids.add(package["set_id"])
    sets = _dicts(c.execute("SELECT * FROM topik_bank.question_sets WHERE set_id=ANY(%s::uuid[])", (sorted(set_ids),)))
    if not sets:
        return {"status": "absent", "set_id": package["set_id"], "batch_id": package["batch_id"]}
    if len(sets) != 1:
        raise PublicationBlocked("문항이 여러 로컬 PostgreSQL 세트에 연결돼 있습니다.")
    existing = sets[0]
    expected_set = dict(exam_format=draft.exam_format, section=draft.section, generator_provider=draft.backend,
                        generator_model=draft.provider_key, generator_version=draft.model_id,
                        review_status=review_status, default_target_level=draft.default_target_level)
    set_status = existing["review_status"]
    if allow_pending and set_status == "pending_review" and review_status == "reviewed":
        pending = True
        set_status = review_status
    if any((set_status if k == "review_status" else existing[k]) != v for k, v in expected_set.items()) or not existing["published_at"]:
        raise PublicationBlocked("로컬 PostgreSQL 세트 메타데이터가 다릅니다.")
    members = _dicts(c.execute("""SELECT position,item_id FROM topik_bank.items
        WHERE set_id=%s ORDER BY position""", (existing["set_id"],)))
    if len(members) != question_count(draft.exam_format) or len(item_ids) != question_count(draft.exam_format) or any(
        r["position"] != position or r["item_id"] != item_ids.get(position)
        for position, r in enumerate(members, 1)
    ):
        raise PublicationBlocked("로컬 PostgreSQL 세트의 번호·문항 연결이 다릅니다.")
    # Verify the actual consumer view, not only underlying tables.
    view = _dicts(c.execute("SELECT position,item_id,content_json FROM topik_bank.current_set_contents WHERE set_id=%s ORDER BY position", (existing["set_id"],)))
    if len(view) != question_count(draft.exam_format) or any(
        r["position"] != i.candidate.slot or r["item_id"] != item_ids[i.candidate.slot]
        or r["content_json"] != i.candidate.question for r, i in zip(view, draft.items)
    ):
        raise PublicationBlocked("로컬 PostgreSQL 조회 뷰에서 전체 문항 내용이 일치하지 않습니다.")
    if "media" in package:
        from topik_question_lab.common.media.media_registration import check_media
        check_media(c, package)
    return {"status": "pending_review" if pending else "complete", "set_id": str(existing["set_id"]), "batch_id": package["batch_id"],
            "section": draft.section, "set_sequence": existing["set_sequence"], "exam_format": draft.exam_format, "verified_items": question_count(draft.exam_format)}


class DirectPublicationService:
    def __init__(self, root, target_url, *, readonly=False):
        self.root = Path(root)
        self.target_url = target_url.strip()
        try:
            self.target = _target_fingerprint(self.target_url)
        except Exception as exc:
            raise PublicationBlocked("로컬 PostgreSQL 연결 문자열 형식이 올바르지 않습니다.") from exc
        self.jobs = PublicationJobs(self.root, readonly=readonly)

    def connect(self, readonly=False):
        if not self.target_url:
            raise PublicationBlocked("DATABASE_URL이 설정되지 않았습니다.")
        psycopg, _ = _psycopg()
        return psycopg.connect(self.target_url, connect_timeout=8, prepare_threshold=None,
            options=f"-c default_transaction_read_only={'on' if readonly else 'off'} -c statement_timeout=60000 -c lock_timeout=15000")

    def catalog(self):
        catalog = collect_candidate_catalog(self.root)
        if catalog.read_errors:
            raise PublicationBlocked(" / ".join(catalog.read_errors))
        return catalog

    def list_batches(self):
        catalog = self.catalog()
        jobs = [j for j in self.jobs.list() if j["target"] == self.target]
        result = []
        for batch in read_batches(self.root):
            records = [r for r in catalog.records if r.candidate.batch_id == batch["id"]]
            row = {**batch, "count": len(records), "approved": sum(bool(r.candidate.review.get("approved")) for r in records),
                   "publication_status": "미등록", "reason": ""}
            try:
                validate_batch(batch, records)
                build_set_draft([r.candidate for r in records], {}, 4, 0.0)
            except (ValueError, KeyError, TypeError) as exc:
                row["reason"] = str(exc)
                row["publication_status"] = "확인 필요"
            source_hash = _hash([asdict(r.candidate) for r in sorted(records, key=lambda r: r.candidate.slot)])
            for job in jobs:
                package = next((p for p in job["payload"]["packages"] if p["batch_id"] == batch["id"]), None)
                if package:
                    row["plan_id"] = job["id"]
                    row["publication_status"] = {"prepared": "검증 완료", "publishing": "결과 확인 필요", "outcome_unknown": "결과 확인 필요",
                        "succeeded": "등록 완료", "failed": "재시도 가능", "conflict": "충돌", "stale": "재검증 필요"}.get(job["status"], job["status"])
                    if source_hash != package["source_hash"]:
                        row["publication_status"] = "등록 후 변경됨" if job["status"] == "succeeded" else "재검증 필요"
                    elif package.get("review_status") == "pending_review" and job["status"] == "succeeded":
                        row["publication_status"] = "등록 완료 · 검수 대기"
                    break
            result.append(row)
        return result

    def prepare(self, batch_ids, metadata=None):
        if not batch_ids:
            raise PublicationBlocked("세트를 선택하세요.")
        catalog = self.catalog()
        batches = {b["id"]: b for b in read_batches(self.root)}
        packages = []
        for batch_id in sorted(set(batch_ids)):
            if batch_id not in batches:
                raise PublicationBlocked("선택한 생성 묶음이 없습니다.")
            packages.append(make_package(batches[batch_id], [r for r in catalog.records if r.candidate.batch_id == batch_id], metadata))
        try:
            with self.connect(readonly=True) as c:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                require_target_schema(c)
                for p in packages:
                    check_registered_set(c, p, allow_pending=True)
        except Exception as exc:
            raise PublicationBlocked(_safe_error(exc, self.target_url)) from exc
        return self.jobs.save(self.target, packages)

    def _job(self, plan_id):
        job = self.jobs.get(plan_id)
        if job["target"] != self.target:
            raise PublicationBlocked("사전 검증 이후 로컬 PostgreSQL 연결 대상이 변경되었습니다.")
        return job

    def _check_current(self, job):
        catalog = self.catalog()
        batches = {b["id"]: b for b in read_batches(self.root)}
        for p in job["payload"]["packages"]:
            batch = batches.get(p["batch_id"])
            if not batch:
                raise PublicationBlocked("생성 묶음이 삭제되었습니다. 재검증하세요.")
            current = make_package(batch, [r for r in catalog.records if r.candidate.batch_id == p["batch_id"]], p["metadata"])
            if _hash(current) != _hash(p):
                raise PublicationBlocked("사전 검증 이후 문항·승인·설정이 변경되었습니다. 재검증하세요.")

    def reconcile(self, plan_id):
        job = self._job(plan_id)
        try:
            with self.connect(readonly=True) as c:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                require_target_schema(c)
                results = [check_registered_set(c, p) for p in job["payload"]["packages"]]
            status = "succeeded" if all(r["status"] == "complete" for r in results) else "failed"
            self.jobs.update(plan_id, status, {"sets": results})
        except PublicationBlocked as exc:
            self.jobs.update(plan_id, "conflict", message=_safe_error(exc, self.target_url))
        except Exception as exc:
            self.jobs.update(plan_id, "outcome_unknown", message=_safe_error(exc, self.target_url))
        return self.jobs.get(plan_id)

    def publish(self, plan_id):
        job = self._job(plan_id)
        try:
            self._check_current(job)
        except (ValueError, KeyError, TypeError) as exc:
            self.jobs.update(plan_id, "stale", message=str(exc))
            raise PublicationBlocked(str(exc)) from exc
        if job["status"] in {"publishing", "outcome_unknown", "succeeded"}:
            checked = self.reconcile(plan_id)
            if checked["status"] == "succeeded":
                return checked
            if checked["status"] != "failed":
                raise PublicationBlocked(checked["message"] or "로컬 PostgreSQL 반영 결과를 먼저 확인해야 합니다.")
        self.jobs.update(plan_id, "publishing")
        try:
            with self.connect() as c:
                # Use the same lock order as the other local publication entry points.
                for lock in ("topik_bank:production_deployment", "topik_bank:set_membership", "topik_bank:item_sync"):
                    c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
                require_target_schema(c)
                # Lock acquisition may have waited while someone edited SQLite.
                self._check_current(job)
                packages = job["payload"]["packages"]
                statuses = [check_registered_set(c, p, allow_pending=True) for p in packages]
                for p, status in zip(packages, statuses):
                    if status["status"] == "absent":
                        self._insert(c, p)
                    elif status["status"] == "pending_review":
                        c.execute("UPDATE topik_bank.items SET review_status='reviewed',updated_at=CURRENT_TIMESTAMP WHERE set_id=%s", (p["set_id"],))
                        c.execute("UPDATE topik_bank.question_sets SET review_status='reviewed' WHERE set_id=%s", (p["set_id"],))
                for p in packages:
                    if check_registered_set(c, p)["status"] != "complete":
                        raise PublicationBlocked("등록 후 세트 검증에 실패했습니다.")
            return self.reconcile(plan_id)
        except Exception as exc:
            message = _safe_error(exc, self.target_url)
            result = self.reconcile(plan_id)
            if result["status"] != "succeeded":
                self.jobs.update(plan_id, result["status"], result["result"], message)
                raise PublicationBlocked(message) from exc
            return result

    def _insert(self, c, package):
        draft = package_draft(package)
        _, Jsonb = _psycopg()
        item_ids = []
        for item in draft.items:
            item_id, _ = _upsert_item(c, item, Jsonb, review_status=package.get("review_status", "reviewed"))
            item_ids.append(item_id)
        identity = (draft.section, draft.backend, draft.provider_key, draft.model_id)
        sequence = c.execute("""SELECT COALESCE(MAX(set_sequence),0)+1 FROM topik_bank.question_sets
            WHERE section=%s AND generator_provider=%s AND generator_model=%s AND generator_version=%s AND exam_format=%s""", (*identity, draft.exam_format)).fetchone()[0]
        c.execute("""INSERT INTO topik_bank.question_sets(set_id,section,generator_provider,generator_model,
            generator_version,set_sequence,review_status,default_target_level,exam_format)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (package["set_id"], *identity, sequence, package.get("review_status", "reviewed"), package["level"], draft.exam_format))
        _insert_set_items(c, [(package["set_id"], pos, item_id) for pos, item_id in enumerate(item_ids, 1)])
