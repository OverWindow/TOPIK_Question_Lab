from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .postgres_storage import (
    MIGRATION_DIR,
    PostgresUnavailableError,
    _psycopg,
    _uses_item_only_set_schema,
)


SYNCED = "연동 완료"
MISSING = "운영 미반영"
PARTIAL = "부분 연동"
CONFLICT = "충돌"
TARGET_ONLY = "운영에만 존재"

REQUIRED_MIGRATIONS = tuple(path.stem for path in sorted(MIGRATION_DIR.glob("*.sql")))
TIMESTAMP_COLUMNS = {
    "items": {"created_at"},
    "item_versions": {"created_at"},
    "question_sets": {"created_at", "published_at"},
}


@dataclass(frozen=True)
class DatabaseStatus:
    configured: bool
    reachable: bool
    label: str
    database_name: str = ""
    migrations: tuple[str, ...] = ()
    item_count: int = 0
    item_version_count: int = 0
    set_count: int = 0
    ready: bool = False
    deployment_ready: bool = False
    schema_model: str = ""
    message: str = ""


@dataclass(frozen=True)
class SetSyncStatus:
    set_id: str
    section: str
    generator_provider: str
    generator_model: str
    generator_version: str
    set_sequence: int
    status: str
    expected_memberships: int
    exact_memberships: int
    missing_rows: int
    conflict_rows: int
    snapshot_hash: str
    reasons: tuple[str, ...] = ()

    @property
    def deployable(self) -> bool:
        return self.status in {MISSING, PARTIAL}


@dataclass(frozen=True)
class DeploymentPlan:
    set_ids: tuple[str, ...]
    statuses: tuple[SetSyncStatus, ...]
    source_snapshot_hash: str
    created_row_count: int
    reused_row_count: int

    @property
    def has_conflicts(self) -> bool:
        return any(value.status == CONFLICT for value in self.statuses)


@dataclass(frozen=True)
class DeploymentReceipt:
    run_id: str
    status: str
    requested_set_count: int
    transferred_set_count: int
    reused_set_count: int
    created_row_count: int
    reused_row_count: int
    message: str = ""


class DeploymentError(RuntimeError):
    def __init__(self, message: str, run_id: str = "", status: str = "rolled_back"):
        super().__init__(message)
        self.run_id = run_id
        self.status = status


@dataclass
class _DatabaseSnapshot:
    sets: dict[str, dict]
    set_id_by_identity: dict[tuple[str, str, str, str, int], str]
    memberships: dict[tuple[str, int], dict]
    membership_position_by_item: dict[tuple[str, str], int]
    items: dict[str, dict]
    item_id_by_source: dict[str, str]
    item_versions: dict[tuple[str, int], dict]
    item_version_by_hash: dict[tuple[str, str], int]


def database_label(database_url: str) -> str:
    if not database_url.strip():
        return "미설정"
    try:
        parsed = urlsplit(database_url)
        host = parsed.hostname or "unknown-host"
        port = f":{parsed.port}" if parsed.port else ""
        database = unquote(parsed.path.lstrip("/")) or "unknown-database"
        return f"{host}{port}/{database}"
    except (TypeError, ValueError):
        return "설정된 PostgreSQL"


def database_fingerprint(database_url: str) -> str:
    try:
        parsed = urlsplit(database_url)
        identity = {
            "host": (parsed.hostname or "").lower(),
            "port": parsed.port,
            "database": unquote(parsed.path.lstrip("/")),
            "user": unquote(parsed.username or ""),
        }
    except (TypeError, ValueError):
        identity = {"url": database_url.strip()}
    return _hash_value(identity)


def same_database_configuration(source_url: str, target_url: str) -> bool:
    return bool(source_url.strip() and target_url.strip()) and (
        database_fingerprint(source_url) == database_fingerprint(target_url)
    )


def inspect_database(database_url: str) -> DatabaseStatus:
    label = database_label(database_url)
    if not database_url.strip():
        return DatabaseStatus(False, False, label, message="연결 문자열이 설정되지 않았습니다.")
    psycopg, _ = _psycopg()
    try:
        with psycopg.connect(
            database_url, connect_timeout=8, prepare_threshold=None
        ) as connection:
            database_name = str(connection.execute("SELECT current_database()").fetchone()[0])
            has_schema = bool(
                connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'topik_bank')"
                ).fetchone()[0]
            )
            if not has_schema:
                return DatabaseStatus(
                    True, True, label, database_name=database_name,
                    message="topik_bank 스키마가 없습니다.",
                )
            has_migrations = _table_exists(connection, "schema_migrations")
            migrations = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT version FROM topik_bank.schema_migrations ORDER BY version"
                ).fetchall()
            ) if has_migrations else ()
            counts = {
                table: _table_count(connection, table)
                for table in ("items", "item_versions", "question_sets")
            }
            effective_migrations = set(migrations)
            current_schema = _uses_item_only_set_schema(connection)
            legacy_schema = _uses_legacy_set_schema(connection)
            if current_schema:
                effective_migrations.add("005_item_only_question_versions")
            missing = [value for value in REQUIRED_MIGRATIONS if value not in effective_migrations]
            structure_problems = _required_structure_problems(connection)
            readiness_messages = []
            if missing and not legacy_schema:
                readiness_messages.append(f"미적용 마이그레이션: {', '.join(missing)}")
            if structure_problems and not legacy_schema:
                readiness_messages.append("구조 확인 필요: " + ", ".join(structure_problems))
            if legacy_schema:
                readiness_messages.append(
                    "구 세트 버전 구조입니다. 운영 배포 원본으로는 사용할 수 있지만 "
                    "새 세트 발행 전에는 Unigate-Web 016 마이그레이션을 적용해야 합니다."
                )
            return DatabaseStatus(
                configured=True,
                reachable=True,
                label=label,
                database_name=database_name,
                migrations=migrations,
                item_count=counts["items"],
                item_version_count=counts["item_versions"],
                set_count=counts["question_sets"],
                ready=current_schema and not missing and not structure_problems,
                deployment_ready=current_schema or legacy_schema,
                schema_model="item_only" if current_schema else "legacy" if legacy_schema else "unknown",
                message=(" / ".join(readiness_messages) if readiness_messages else "스키마가 최신입니다."),
            )
    except Exception as exc:
        return DatabaseStatus(
            True, False, label,
            message=f"연결 실패: {_safe_error(exc, database_url)}",
        )


class PostgresDeploymentService:
    def __init__(self, source_url: str, target_url: str):
        if not source_url.strip():
            raise ValueError("DATABASE_URL이 설정되지 않았습니다.")
        if not target_url.strip():
            raise ValueError("PRODUCTION_DATABASE_URL이 설정되지 않았습니다.")
        if same_database_configuration(source_url, target_url):
            raise ValueError("원본과 운영 대상이 같은 PostgreSQL 연결입니다.")
        self.source_url = source_url.strip()
        self.target_url = target_url.strip()

    def compare(self) -> list[SetSyncStatus]:
        source, target = self._snapshots()
        return compare_snapshots(source, target)

    def preview(self, set_ids: Iterable[str]) -> DeploymentPlan:
        selected = _normalized_set_ids(set_ids)
        source, target = self._snapshots()
        return _build_plan(source, target, selected)

    def deploy(self, set_ids: Iterable[str]) -> DeploymentReceipt:
        selected = _normalized_set_ids(set_ids)
        psycopg, Jsonb = _psycopg()
        run_id = ""
        baseline_digest = ""
        created_rows = 0
        reused_rows = 0
        initial_plan: DeploymentPlan | None = None
        source: _DatabaseSnapshot | None = None
        try:
            with psycopg.connect(self.source_url) as source_connection:
                source_connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                _require_deployment_source_schema(source_connection)
                source = _load_snapshot(source_connection)
            with psycopg.connect(
                self.target_url, prepare_threshold=None
            ) as target_connection:
                _require_current_target_schema(target_connection)
                target = _load_snapshot(target_connection)
            initial_plan = _build_plan(source, target, selected)
            run_id = self._start_audit(source, initial_plan)
            if initial_plan.has_conflicts:
                reasons = [reason for status in initial_plan.statuses for reason in status.reasons]
                raise ValueError("운영 DB 충돌: " + "; ".join(reasons[:8]))

            with psycopg.connect(
                self.target_url, prepare_threshold=None
            ) as target_connection:
                target_connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('topik_bank:production_deployment'))"
                )
                current_target = _load_snapshot(target_connection)
                current_plan = _build_plan(source, current_target, selected)
                baseline_digest = _target_state_digest(source, current_target, selected)
                if current_plan.has_conflicts:
                    reasons = [reason for status in current_plan.statuses for reason in status.reasons]
                    raise ValueError("사전 검증 이후 운영 DB가 변경되었습니다: " + "; ".join(reasons[:8]))
                created_rows = _copy_missing_rows(
                    target_connection, source, current_target, selected, Jsonb
                )
                expected_rows = _expected_row_count(source, selected)
                reused_rows = expected_rows - created_rows
                verified_target = _load_snapshot(target_connection)
                verified = _build_plan(source, verified_target, selected)
                if any(value.status != SYNCED for value in verified.statuses):
                    failures = [
                        f"{value.set_id}: {value.status}"
                        for value in verified.statuses if value.status != SYNCED
                    ]
                    raise RuntimeError("배포 후 검증 실패: " + ", ".join(failures))

            transferred = sum(value.status != SYNCED for value in initial_plan.statuses)
            reused_sets = len(selected) - transferred
            receipt = DeploymentReceipt(
                run_id=run_id,
                status="succeeded",
                requested_set_count=len(selected),
                transferred_set_count=transferred,
                reused_set_count=reused_sets,
                created_row_count=created_rows,
                reused_row_count=reused_rows,
            )
            self._finish_audit(receipt)
            return receipt
        except Exception as exc:
            if not run_id:
                if isinstance(exc, (ValueError, DeploymentError)):
                    raise
                raise DeploymentError(
                    "운영 배포를 시작하지 못했습니다: "
                    f"{_safe_error(exc, self.source_url, self.target_url)}"
                ) from exc
            outcome = "rolled_back"
            message = _safe_error(exc, self.source_url, self.target_url)
            try:
                assert source is not None
                with psycopg.connect(
                    self.target_url, prepare_threshold=None
                ) as target_connection:
                    after = _load_snapshot(target_connection)
                after_plan = _build_plan(source, after, selected)
                if all(value.status == SYNCED for value in after_plan.statuses):
                    outcome = "succeeded"
                elif baseline_digest and _target_state_digest(source, after, selected) != baseline_digest:
                    outcome = "outcome_unknown"
            except Exception as verify_exc:
                outcome = "outcome_unknown"
                message = (
                    f"{message} / 결과 재확인 실패: "
                    f"{_safe_error(verify_exc, self.source_url, self.target_url)}"
                )

            transferred = 0
            reused_sets = 0
            if initial_plan:
                reused_sets = sum(value.status == SYNCED for value in initial_plan.statuses)
                if outcome == "succeeded":
                    transferred = len(selected) - reused_sets
            receipt = DeploymentReceipt(
                run_id=run_id,
                status=outcome,
                requested_set_count=len(selected),
                transferred_set_count=transferred,
                reused_set_count=reused_sets,
                created_row_count=created_rows if outcome == "succeeded" else 0,
                reused_row_count=reused_rows if outcome == "succeeded" else 0,
                message=message,
            )
            self._finish_audit(receipt)
            if outcome == "succeeded":
                return receipt
            label = "전체 롤백 완료" if outcome == "rolled_back" else "결과 확인 필요"
            raise DeploymentError(
                f"{label} · run_id={run_id}: {message}", run_id=run_id, status=outcome
            ) from exc

    def list_deployment_runs(self, limit: int = 50) -> list[dict]:
        psycopg, _ = _psycopg()
        try:
            with psycopg.connect(self.source_url) as connection:
                cursor = connection.execute(
                    """SELECT r.run_id, r.target_label, r.status, r.requested_set_count,
                              r.transferred_set_count, r.reused_set_count,
                              r.created_row_count, r.reused_row_count, r.error_message,
                              r.started_at, r.completed_at,
                              COUNT(s.set_id) AS audited_set_count
                       FROM topik_bank.deployment_runs r
                       LEFT JOIN topik_bank.deployment_run_sets s ON s.run_id = r.run_id
                       GROUP BY r.run_id
                       ORDER BY r.started_at DESC
                       LIMIT %s""",
                    (limit,),
                )
                return _dict_rows(cursor)
        except Exception as exc:
            raise PostgresUnavailableError(
                f"운영 배포 이력을 읽지 못했습니다: {_safe_error(exc, self.source_url)}"
            ) from exc

    def _snapshots(self) -> tuple[_DatabaseSnapshot, _DatabaseSnapshot]:
        psycopg, _ = _psycopg()
        try:
            with psycopg.connect(self.source_url) as source_connection:
                _require_deployment_source_schema(source_connection)
                source = _load_snapshot(source_connection)
            with psycopg.connect(
                self.target_url, prepare_threshold=None
            ) as target_connection:
                _require_current_target_schema(target_connection)
                target = _load_snapshot(target_connection)
            return source, target
        except Exception as exc:
            raise PostgresUnavailableError(
                "원본·운영 DB 상태를 비교하지 못했습니다: "
                f"{_safe_error(exc, self.source_url, self.target_url)}"
            ) from exc

    def _start_audit(self, source: _DatabaseSnapshot, plan: DeploymentPlan) -> str:
        psycopg, _ = _psycopg()
        run_id = str(uuid.uuid4())
        target_label = database_label(self.target_url)
        target_fingerprint = database_fingerprint(self.target_url)
        try:
            with psycopg.connect(self.source_url) as connection:
                connection.execute(
                    """INSERT INTO topik_bank.deployment_runs(
                           run_id, target_fingerprint, target_label, source_snapshot_hash,
                           status, requested_set_count
                       ) VALUES (%s, %s, %s, %s, 'running', %s)""",
                    (run_id, target_fingerprint, target_label, plan.source_snapshot_hash, len(plan.set_ids)),
                )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO topik_bank.deployment_run_sets(
                               run_id, set_id, set_sequence, source_snapshot_hash, status
                           ) VALUES (%s, %s, %s, %s, 'running')""",
                        [
                            (run_id, status.set_id, status.set_sequence, status.snapshot_hash)
                            for status in plan.statuses
                        ],
                    )
            return run_id
        except Exception as exc:
            raise DeploymentError(
                f"로컬 배포 이력을 시작하지 못했습니다: {_safe_error(exc, self.source_url)}"
            ) from exc

    def _finish_audit(self, receipt: DeploymentReceipt) -> None:
        psycopg, _ = _psycopg()
        detail = receipt.message[:4000]
        try:
            with psycopg.connect(self.source_url) as connection:
                connection.execute(
                    """UPDATE topik_bank.deployment_runs
                       SET status = %s, transferred_set_count = %s, reused_set_count = %s,
                           created_row_count = %s, reused_row_count = %s,
                           error_message = %s, completed_at = CURRENT_TIMESTAMP
                       WHERE run_id = %s""",
                    (
                        receipt.status, receipt.transferred_set_count, receipt.reused_set_count,
                        receipt.created_row_count, receipt.reused_row_count, detail, receipt.run_id,
                    ),
                )
                connection.execute(
                    """UPDATE topik_bank.deployment_run_sets
                       SET status = %s, detail = %s WHERE run_id = %s""",
                    (receipt.status, detail, receipt.run_id),
                )
        except Exception as exc:
            if receipt.status == "succeeded":
                raise DeploymentError(
                    "운영 배포는 완료됐지만 로컬 이력 갱신에 실패했습니다: "
                    f"{_safe_error(exc, self.source_url)}",
                    run_id=receipt.run_id,
                    status="outcome_unknown",
                ) from exc


def compare_snapshots(source: _DatabaseSnapshot, target: _DatabaseSnapshot) -> list[SetSyncStatus]:
    statuses = [_compare_set(source, target, set_id) for set_id in source.sets]
    for set_id, row in target.sets.items():
        if set_id in source.sets:
            continue
        memberships = [key for key in target.memberships if key[0] == set_id]
        statuses.append(
            SetSyncStatus(
                set_id=set_id,
                section=str(row["section"]),
                generator_provider=str(row["generator_provider"]),
                generator_model=str(row["generator_model"]),
                generator_version=str(row["generator_version"]),
                set_sequence=int(row["set_sequence"]),
                status=TARGET_ONLY,
                expected_memberships=len(memberships),
                exact_memberships=len(memberships),
                missing_rows=0,
                conflict_rows=0,
                snapshot_hash="",
                reasons=("운영 DB에만 존재하는 세트입니다.",),
            )
        )
    return sorted(
        statuses,
        key=lambda value: (
            value.section, value.generator_version, value.set_sequence, value.set_id
        ),
    )


def _compare_set(source: _DatabaseSnapshot, target: _DatabaseSnapshot, set_id: str) -> SetSyncStatus:
    source_set = source.sets[set_id]
    missing: list[str] = []
    conflicts: list[str] = []
    exact_rows = 0
    expected_rows = _rows_for_set(source, set_id)

    target_set = target.sets.get(set_id)
    identity = _set_identity(source_set)
    if target_set is None:
        other_id = target.set_id_by_identity.get(identity)
        if other_id:
            conflicts.append(f"같은 모델·영역·세트 번호가 다른 set_id({other_id})로 존재합니다.")
        else:
            missing.append("question_sets 행이 없습니다.")
    elif _rows_equal("question_sets", source_set, target_set):
        exact_rows += 1
    else:
        conflicts.append("question_sets 내용이 다릅니다.")

    for row in expected_rows["items"]:
        key = str(row["item_id"])
        actual = target.items.get(key)
        if actual is None:
            other = target.item_id_by_source.get(str(row["source_key"]))
            if other:
                conflicts.append(f"source_key {row['source_key']}가 다른 item_id({other})를 사용합니다.")
            else:
                missing.append(f"item {key}가 없습니다.")
        elif _rows_equal("items", row, actual):
            exact_rows += 1
        else:
            conflicts.append(f"item {key} 내용이 다릅니다.")

    for row in expected_rows["item_versions"]:
        key = (str(row["item_id"]), int(row["item_version"]))
        actual = target.item_versions.get(key)
        if actual is None:
            other = target.item_version_by_hash.get((key[0], str(row["content_hash"])))
            if other is not None:
                conflicts.append(f"item {key[0]}의 동일 content_hash가 버전 {other}에 존재합니다.")
            else:
                missing.append(f"item_version {key[0]} v{key[1]}이 없습니다.")
        elif _rows_equal("item_versions", row, actual):
            exact_rows += 1
        else:
            conflicts.append(f"item_version {key[0]} v{key[1]} 내용이 다릅니다.")

    exact_memberships = 0
    for row in expected_rows["question_set_items"]:
        key = (str(row["set_id"]), int(row["position"]))
        actual = target.memberships.get(key)
        if actual is None:
            other_position = target.membership_position_by_item.get(
                (key[0], str(row["item_id"]))
            )
            if other_position is not None:
                conflicts.append(
                    f"세트 {key[0]}의 item {row['item_id']}가 다른 위치 {other_position}에 있습니다."
                )
            else:
                missing.append(f"세트 {key[0]} {key[1]}번 문항이 없습니다.")
        elif _rows_equal("question_set_items", row, actual):
            exact_rows += 1
            exact_memberships += 1
        else:
            conflicts.append(f"세트 {key[0]} {key[1]}번 문항이 다릅니다.")

    expected_membership_keys = {
        (str(row["set_id"]), int(row["position"]))
        for row in expected_rows["question_set_items"]
    }
    target_membership_keys = {key for key in target.memberships if key[0] == set_id}
    for extra in sorted(target_membership_keys - expected_membership_keys):
        conflicts.append(f"운영 DB에만 세트 {extra[1]}번 연결이 존재합니다.")

    if conflicts:
        status = CONFLICT
    elif not missing:
        status = SYNCED
    elif target_set is None:
        status = MISSING
    else:
        status = PARTIAL
    return SetSyncStatus(
        set_id=set_id,
        section=str(source_set["section"]),
        generator_provider=str(source_set["generator_provider"]),
        generator_model=str(source_set["generator_model"]),
        generator_version=str(source_set["generator_version"]),
        set_sequence=int(source_set["set_sequence"]),
        status=status,
        expected_memberships=len(expected_rows["question_set_items"]),
        exact_memberships=exact_memberships,
        missing_rows=len(missing),
        conflict_rows=len(conflicts),
        snapshot_hash=_snapshot_hash(source, set_id),
        reasons=tuple((conflicts or missing)[:20]),
    )


def _build_plan(
    source: _DatabaseSnapshot, target: _DatabaseSnapshot, set_ids: tuple[str, ...]
) -> DeploymentPlan:
    missing_ids = [set_id for set_id in set_ids if set_id not in source.sets]
    if missing_ids:
        raise ValueError(f"로컬 원본에 없는 set_id입니다: {', '.join(missing_ids)}")
    statuses = tuple(_compare_set(source, target, set_id) for set_id in set_ids)
    expected = _expected_row_count(source, set_ids)
    missing_rows = _missing_unique_row_count(source, target, set_ids)
    snapshot_hash = _hash_value([value.snapshot_hash for value in statuses])
    return DeploymentPlan(
        set_ids=set_ids,
        statuses=statuses,
        source_snapshot_hash=snapshot_hash,
        created_row_count=missing_rows,
        reused_row_count=max(0, expected - missing_rows),
    )


def _load_snapshot(connection) -> _DatabaseSnapshot:
    if _uses_legacy_set_schema(connection):
        sets = _indexed_rows(
            connection,
            """SELECT s.set_id, s.section, s.generator_provider, s.generator_model,
                      s.generator_version, s.set_sequence, v.review_status,
                      v.default_target_level, v.default_predicted_difficulty,
                      v.set_fingerprint, v.published_at, s.created_at
               FROM topik_bank.question_sets s
               JOIN LATERAL (
                   SELECT review_status, default_target_level,
                          default_predicted_difficulty, set_fingerprint,
                          published_at, set_version
                     FROM topik_bank.question_set_versions
                    WHERE set_id = s.set_id
                    ORDER BY set_version DESC
                    LIMIT 1
               ) v ON TRUE""",
            lambda row: str(row["set_id"]),
        )
        memberships = _indexed_rows(
            connection,
            """WITH latest AS (
                   SELECT set_id, MAX(set_version) AS set_version
                     FROM topik_bank.question_set_versions
                    GROUP BY set_id
               )
               SELECT member.set_id, member.position, member.item_id,
                      member.item_version
                 FROM topik_bank.question_set_items member
                 JOIN latest
                   ON latest.set_id=member.set_id
                  AND latest.set_version=member.set_version""",
            lambda row: (str(row["set_id"]), int(row["position"])),
        )
    else:
        sets = _indexed_rows(
            connection,
            """SELECT set_id, section, generator_provider, generator_model,
                      generator_version, set_sequence, review_status,
                      default_target_level, default_predicted_difficulty,
                      set_fingerprint, published_at, created_at
               FROM topik_bank.question_sets""",
            lambda row: str(row["set_id"]),
        )
        memberships = _indexed_rows(
            connection,
            """SELECT set_id, position, item_id, item_version
               FROM topik_bank.question_set_items""",
            lambda row: (str(row["set_id"]), int(row["position"])),
        )
    items = _indexed_rows(
        connection,
        "SELECT item_id, source_key, created_at FROM topik_bank.items",
        lambda row: str(row["item_id"]),
    )
    item_versions = _indexed_rows(
        connection,
        """SELECT item_id, item_version, section, type_slot, item_type, primary_skill,
                  target_level, predicted_difficulty, irt_difficulty, irt_discrimination,
                  stem_length, choice_count, generator_provider, generator_model,
                  generator_version, prompt_version, review_status, stem, choices,
                  correct_answer, explanation, content_json, source_provenance,
                  content_hash, created_at
           FROM topik_bank.item_versions""",
        lambda row: (str(row["item_id"]), int(row["item_version"])),
    )
    return _DatabaseSnapshot(
        sets=sets,
        set_id_by_identity={_set_identity(row): key for key, row in sets.items()},
        memberships=memberships,
        membership_position_by_item={
            (key[0], str(row["item_id"])): key[1]
            for key, row in memberships.items()
        },
        items=items,
        item_id_by_source={str(row["source_key"]): key for key, row in items.items()},
        item_versions=item_versions,
        item_version_by_hash={
            (key[0], str(row["content_hash"])): key[1]
            for key, row in item_versions.items()
        },
    )


def _rows_for_set(snapshot: _DatabaseSnapshot, set_id: str) -> dict[str, list[dict]]:
    memberships = [row for key, row in snapshot.memberships.items() if key[0] == set_id]
    item_version_keys = {
        (str(row["item_id"]), int(row["item_version"])) for row in memberships
    }
    item_ids = {key[0] for key in item_version_keys}
    return {
        "question_sets": [snapshot.sets[set_id]],
        "question_set_items": sorted(
            memberships, key=lambda row: int(row["position"])
        ),
        "items": [snapshot.items[item_id] for item_id in sorted(item_ids)],
        "item_versions": [
            snapshot.item_versions[key] for key in sorted(item_version_keys)
        ],
    }


def _copy_missing_rows(connection, source, target, set_ids, Jsonb) -> int:
    rows = _merged_rows(source, set_ids)
    created = 0
    for row in rows["items"]:
        if str(row["item_id"]) in target.items:
            continue
        created += _execute_insert(
            connection,
            """INSERT INTO topik_bank.items(item_id, source_key, created_at)
               VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
            (row["item_id"], row["source_key"], row["created_at"]),
        )
    for row in rows["item_versions"]:
        key = (str(row["item_id"]), int(row["item_version"]))
        if key in target.item_versions:
            continue
        created += _execute_insert(
            connection,
            """INSERT INTO topik_bank.item_versions(
                   item_id, item_version, section, type_slot, item_type, primary_skill,
                   target_level, predicted_difficulty, irt_difficulty, irt_discrimination,
                   stem_length, choice_count, generator_provider, generator_model,
                   generator_version, prompt_version, review_status, stem, choices,
                   correct_answer, explanation, content_json, source_provenance,
                   content_hash, created_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (
                row["item_id"], row["item_version"], row["section"], row["type_slot"],
                row["item_type"], row["primary_skill"], row["target_level"],
                row["predicted_difficulty"], row["irt_difficulty"], row["irt_discrimination"],
                row["stem_length"], row["choice_count"], row["generator_provider"],
                row["generator_model"], row["generator_version"], row["prompt_version"],
                row["review_status"], row["stem"], Jsonb(row["choices"]), row["correct_answer"],
                row["explanation"], Jsonb(row["content_json"]), Jsonb(row["source_provenance"]),
                row["content_hash"], row["created_at"],
            ),
        )
    for row in rows["question_sets"]:
        if str(row["set_id"]) in target.sets:
            continue
        created += _execute_insert(
            connection,
            """INSERT INTO topik_bank.question_sets(
                   set_id, section, generator_provider, generator_model,
                   generator_version, set_sequence, review_status,
                   default_target_level, default_predicted_difficulty,
                   set_fingerprint, published_at, created_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (
                row["set_id"], row["section"], row["generator_provider"],
                row["generator_model"], row["generator_version"], row["set_sequence"],
                row["review_status"], row["default_target_level"],
                row["default_predicted_difficulty"], row["set_fingerprint"],
                row["published_at"], row["created_at"],
            ),
        )
    for row in rows["question_set_items"]:
        key = (str(row["set_id"]), int(row["position"]))
        if key in target.memberships:
            continue
        created += _execute_insert(
            connection,
            """INSERT INTO topik_bank.question_set_items(
                   set_id, position, item_id, item_version
               ) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (
                row["set_id"], row["position"],
                row["item_id"], row["item_version"],
            ),
        )
    return created


def _merged_rows(snapshot: _DatabaseSnapshot, set_ids: Iterable[str]) -> dict[str, list[dict]]:
    result: dict[str, dict[Any, dict]] = {
        "items": {}, "item_versions": {}, "question_sets": {},
        "question_set_items": {},
    }
    key_functions = {
        "items": lambda row: str(row["item_id"]),
        "item_versions": lambda row: (str(row["item_id"]), int(row["item_version"])),
        "question_sets": lambda row: str(row["set_id"]),
        "question_set_items": lambda row: (
            str(row["set_id"]), int(row["position"])
        ),
    }
    for set_id in set_ids:
        for table, rows in _rows_for_set(snapshot, set_id).items():
            for row in rows:
                result[table][key_functions[table](row)] = row
    return {table: list(values.values()) for table, values in result.items()}


def _expected_row_count(snapshot: _DatabaseSnapshot, set_ids: Iterable[str]) -> int:
    return sum(len(values) for values in _merged_rows(snapshot, set_ids).values())


def _missing_unique_row_count(source, target, set_ids: Iterable[str]) -> int:
    return sum(
        _lookup_row(target, table, _row_key(table, row)) is None
        for table, rows in _merged_rows(source, set_ids).items()
        for row in rows
    )


def _snapshot_hash(snapshot: _DatabaseSnapshot, set_id: str) -> str:
    semantic = {
        table: [_semantic_row(table, row) for row in rows]
        for table, rows in _rows_for_set(snapshot, set_id).items()
    }
    return _hash_value(semantic)


def _target_state_digest(source, target, set_ids) -> str:
    state: list[dict[str, Any]] = []
    for set_id in set_ids:
        expected = _rows_for_set(source, set_id)
        for table, rows in expected.items():
            for row in rows:
                key = _row_key(table, row)
                actual = _lookup_row(target, table, key)
                state.append({"table": table, "key": key, "actual": _semantic_row(table, actual) if actual else None})
        for key, row in target.memberships.items():
            if key[0] == set_id and key not in {_row_key("question_set_items", value) for value in expected["question_set_items"]}:
                state.append({"table": "extra_membership", "key": key, "actual": _semantic_row("question_set_items", row)})
    return _hash_value(state)


def _lookup_row(snapshot, table, key):
    mapping = {
        "items": snapshot.items,
        "item_versions": snapshot.item_versions,
        "question_sets": snapshot.sets,
        "question_set_items": snapshot.memberships,
    }[table]
    return mapping.get(key)


def _row_key(table: str, row: dict):
    if table == "items":
        return str(row["item_id"])
    if table == "item_versions":
        return str(row["item_id"]), int(row["item_version"])
    if table == "question_sets":
        return str(row["set_id"])
    return str(row["set_id"]), int(row["position"])


def _set_identity(row: dict) -> tuple[str, str, str, str, int]:
    return (
        str(row["section"]), str(row["generator_provider"]),
        str(row["generator_model"]), str(row["generator_version"]),
        int(row["set_sequence"]),
    )


def _rows_equal(table: str, left: dict, right: dict) -> bool:
    return _semantic_row(table, left) == _semantic_row(table, right)


def _semantic_row(table: str, row: dict) -> dict:
    excluded = TIMESTAMP_COLUMNS.get(table, set())
    return {key: _json_value(value) for key, value in row.items() if key not in excluded}


def _json_value(value):
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _hash_value(value: Any) -> str:
    payload = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _indexed_rows(connection, sql: str, key_function) -> dict:
    cursor = connection.execute(sql)
    rows = _dict_rows(cursor)
    return {key_function(row): row for row in rows}


def _dict_rows(cursor) -> list[dict]:
    columns = [value.name for value in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _execute_insert(connection, sql: str, params: tuple) -> int:
    cursor = connection.execute(sql, params)
    return max(0, int(cursor.rowcount or 0))


def _normalized_set_ids(set_ids: Iterable[str]) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(str(value).strip() for value in set_ids if str(value).strip()))
    if not values:
        raise ValueError("배포할 세트를 하나 이상 선택하세요.")
    return values


def _table_exists(connection, table: str) -> bool:
    return bool(
        connection.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'topik_bank' AND table_name = %s
               )""",
            (table,),
        ).fetchone()[0]
    )


def _uses_legacy_set_schema(connection) -> bool:
    return bool(
        connection.execute(
            """SELECT
                   to_regclass('topik_bank.question_sets') IS NOT NULL
                   AND to_regclass('topik_bank.question_set_items') IS NOT NULL
                   AND to_regclass('topik_bank.question_set_versions') IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM information_schema.columns
                       WHERE table_schema='topik_bank' AND table_name='question_set_items'
                         AND column_name='set_version'
                   )"""
        ).fetchone()[0]
    )


def _require_deployment_source_schema(connection) -> None:
    if _uses_item_only_set_schema(connection) or _uses_legacy_set_schema(connection):
        return
    raise ValueError("로컬 원본의 topik_bank 세트 구조를 인식할 수 없습니다.")


def _require_current_target_schema(connection) -> None:
    if _uses_item_only_set_schema(connection):
        return
    raise ValueError(
        "운영 DB가 현재 item-only 세트 구조가 아닙니다. "
        "Unigate-Web 016 마이그레이션을 먼저 적용하세요."
    )


def _table_count(connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    allowed = {"items", "item_versions", "question_sets"}
    if table not in allowed:
        raise ValueError(f"허용되지 않은 테이블: {table}")
    return int(connection.execute(f"SELECT COUNT(*) FROM topik_bank.{table}").fetchone()[0])


def _required_structure_problems(connection) -> list[str]:
    problems: list[str] = []
    required_tables = {
        "items", "item_versions", "question_sets", "question_set_items",
        "deployment_runs", "deployment_run_sets",
    }
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = 'topik_bank'"""
        ).fetchall()
    }
    for table in sorted(required_tables - existing_tables):
        problems.append(f"테이블 {table}")
    required_columns = {
        ("item_versions", "type_slot"),
        ("question_sets", "set_sequence"),
        ("question_sets", "review_status"),
        ("question_sets", "default_target_level"),
        ("question_sets", "default_predicted_difficulty"),
        ("question_sets", "set_fingerprint"),
        ("question_sets", "published_at"),
    }
    existing_columns = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'topik_bank'"""
        ).fetchall()
    }
    for table, column in sorted(required_columns - existing_columns):
        problems.append(f"컬럼 {table}.{column}")
    if ("question_set_items", "set_version") in existing_columns:
        problems.append("제거되지 않은 컬럼 question_set_items.set_version")
    if "question_set_versions" in existing_tables:
        problems.append("제거되지 않은 테이블 question_set_versions")
    existing_views = {
        str(row[0])
        for row in connection.execute(
            """SELECT table_name FROM information_schema.views
               WHERE table_schema = 'topik_bank'"""
        ).fetchall()
    }
    for view in sorted({"current_items", "current_set_contents"} - existing_views):
        problems.append(f"뷰 {view}")
    has_sequence_constraint = bool(
        connection.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM pg_constraint
                   WHERE conname = 'question_sets_identity_sequence_key'
                     AND conrelid = 'topik_bank.question_sets'::regclass
               )"""
        ).fetchone()[0]
    ) if "question_sets" in existing_tables else False
    if not has_sequence_constraint:
        problems.append("제약 question_sets_identity_sequence_key")
    return problems


def _safe_error(exc: Exception, *database_urls: str) -> str:
    message = str(exc)
    for database_url in database_urls:
        if not database_url:
            continue
        message = message.replace(database_url, "[DATABASE_URL]")
        try:
            password = urlsplit(database_url).password
        except ValueError:
            password = None
        if password:
            message = message.replace(password, "***").replace(unquote(password), "***")
    return message
