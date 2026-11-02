from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from topik_question_lab.step_06_publication.publication import PublicationItemDraft, PublicationSetDraft, validate_complete_selection


MIGRATION_DIR = Path(__file__).resolve().parent / "migrations"


class PostgresUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicationReceipt:
    set_id: str
    set_sequence: int
    created_items: int
    updated_items: int
    reused_items: int
    created_set: bool


@dataclass(frozen=True)
class ItemPublicationReceipt:
    total_items: int
    created_items: int
    updated_items: int
    reused_items: int


class PostgresQuestionBank:
    def __init__(self, database_url: str):
        if not database_url.strip():
            raise ValueError("DATABASE_URL이 설정되지 않았습니다.")
        self.database_url = database_url.strip()

    def check_connection(self) -> str:
        psycopg, _ = _psycopg()
        try:
            with psycopg.connect(self.database_url) as connection:
                return str(connection.execute("SELECT current_database()").fetchone()[0])
        except Exception as exc:
            raise PostgresUnavailableError(
                f"PostgreSQL 연결에 실패했습니다: {_safe_error(exc, self.database_url)}"
            ) from exc

    def ensure_schema(self) -> list[str]:
        psycopg, _ = _psycopg()
        applied: list[str] = []
        try:
            with psycopg.connect(self.database_url) as connection:
                connection.execute("CREATE SCHEMA IF NOT EXISTS topik_bank")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS topik_bank.schema_migrations (
                           version TEXT PRIMARY KEY,
                           applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                       )"""
                )
                unversioned = _uses_unversioned_schema(connection)
                current_set_schema = unversioned or _uses_item_only_set_schema(connection)
                superseded_by_current_schema = {
                    "001_question_bank",
                    "002_complete_item_bank",
                    "003_multi_question_sets",
                    "005_item_only_question_versions",
                }
                if unversioned:
                    superseded_by_current_schema.add("006_unversioned_items")
                for path in sorted(MIGRATION_DIR.glob("*.sql")):
                    version = path.stem
                    exists = connection.execute(
                        "SELECT 1 FROM topik_bank.schema_migrations WHERE version = %s", (version,)
                    ).fetchone()
                    if exists:
                        continue
                    # Respect schema changes applied by the consumer service.
                    if not (
                        current_set_schema and version in superseded_by_current_schema
                    ):
                        if (
                            version == "005_item_only_question_versions"
                            and _has_external_set_version_dependencies(connection)
                        ):
                            raise RuntimeError(
                                "topik_app가 구 세트 버전 구조를 참조하고 있습니다. "
                                "Unigate-Web의 016_item_only_question_versions 마이그레이션을 "
                                "먼저 적용한 후 문항 버전 제거 마이그레이션(006)을 준비하세요."
                            )
                        connection.execute(path.read_text(encoding="utf-8"))
                    connection.execute(
                        "INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (version,)
                    )
                    applied.append(version)
        except Exception as exc:
            raise PostgresUnavailableError(
                f"PostgreSQL 스키마 준비에 실패했습니다: {_safe_error(exc, self.database_url)}"
            ) from exc
        return applied

    def publish_set(self, draft: PublicationSetDraft, *, expected_state=None, validate_source=None) -> PublicationReceipt:
        validate_complete_selection(item.candidate for item in draft.items)
        psycopg, Jsonb = _psycopg()
        try:
            with psycopg.connect(self.database_url) as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:production_deployment'))")
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('topik_bank:set_membership'))"
                )
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"{draft.section}:{draft.backend}:{draft.provider_key}:{draft.model_id}",),
                )
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:item_sync'))")
                _check_expected_state(connection, draft.items, expected_state, validate_source)
                requested_by_position = {
                    item.candidate.slot: item.candidate.source_key for item in draft.items
                }
                membership_rows = _set_memberships_for_sources(
                    connection, list(requested_by_position.values())
                )
                requested_identity = (
                    draft.section,
                    draft.backend,
                    draft.provider_key,
                    draft.model_id,
                )
                existing_membership, conflicts = _resolve_set_membership(
                    membership_rows, requested_by_position, requested_identity
                )
                if existing_membership:
                    set_id, set_sequence = existing_membership
                    actions = {"created": 0, "updated": 0, "reused": 0}
                    for item in draft.items:
                        _, action = _upsert_item(connection, item, Jsonb)
                        actions[action] += 1
                    connection.execute(
                        """UPDATE topik_bank.question_sets
                           SET default_target_level=%s
                           WHERE set_id=%s""",
                        (draft.default_target_level, set_id),
                    )
                    return PublicationReceipt(
                        set_id=set_id,
                        set_sequence=set_sequence,
                        created_items=actions["created"],
                        updated_items=actions["updated"],
                        reused_items=actions["reused"],
                        created_set=False,
                    )
                if conflicts:
                    preview = ", ".join(conflicts[:8])
                    suffix = f" 외 {len(conflicts) - 8}개" if len(conflicts) > 8 else ""
                    raise ValueError(
                        "이미 PostgreSQL 세트에 사용된 문항은 새 세트에 포함할 수 없습니다: "
                        f"{preview}{suffix}"
                    )

                item_ids: list[Any] = []
                created_items = 0
                updated_items = 0
                reused_items = 0
                for item in draft.items:
                    item_id, action = _upsert_item(connection, item, Jsonb)
                    created_items += action == "created"
                    updated_items += action == "updated"
                    reused_items += action == "reused"
                    item_ids.append(item_id)

                set_sequence = int(
                    connection.execute(
                        """SELECT COALESCE(MAX(set_sequence), 0) + 1
                           FROM topik_bank.question_sets
                           WHERE section = %s AND generator_provider = %s
                             AND generator_model = %s AND generator_version = %s""",
                        requested_identity,
                    ).fetchone()[0]
                )
                set_id = draft.set_id_for_sequence(set_sequence)
                connection.execute(
                    """INSERT INTO topik_bank.question_sets(
                           set_id, section, generator_provider, generator_model,
                           generator_version, set_sequence, review_status,
                           default_target_level
                       ) VALUES (%s, %s, %s, %s, %s, %s, 'reviewed', %s)""",
                    (
                        set_id,
                        draft.section,
                        draft.backend,
                        draft.provider_key,
                        draft.model_id,
                        set_sequence,
                        draft.default_target_level,
                    ),
                )
                _insert_set_items(
                    connection,
                    [
                        (set_id, position, item_id)
                        for position, item_id in enumerate(item_ids, start=1)
                    ],
                )
                return PublicationReceipt(
                    set_id=str(set_id),
                    set_sequence=set_sequence,
                    created_items=created_items,
                    updated_items=updated_items,
                    reused_items=reused_items,
                    created_set=True,
                )
        except PostgresUnavailableError:
            raise
        except ValueError:
            raise
        except Exception as exc:
            raise PostgresUnavailableError(
                f"세트 발행에 실패해 전체 작업을 롤백했습니다: {_safe_error(exc, self.database_url)}"
            ) from exc

    def publish_items(self, items: tuple[PublicationItemDraft, ...] | list[PublicationItemDraft], *, expected_state=None, validate_source=None) -> ItemPublicationReceipt:
        values = tuple(items)
        if not values:
            return ItemPublicationReceipt(0, 0, 0, 0)
        psycopg, Jsonb = _psycopg()
        try:
            with psycopg.connect(self.database_url) as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:production_deployment'))")
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:set_membership'))")
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:item_sync'))")
                _check_expected_state(connection, values, expected_state, validate_source)
                created = 0
                updated = 0
                reused = 0
                for item in values:
                    status = (expected_state.get(item.candidate.source_key) or {}).get("review_status", "reviewed") if expected_state is not None else "reviewed"
                    _, action = _upsert_item(connection, item, Jsonb, review_status=status)
                    old = expected_state.get(item.candidate.source_key) if expected_state is not None else None
                    if old and action == "updated" and old["content_hash"] != item.content_hash:
                        _invalidate_set_audio(connection, old)
                    created += action == "created"
                    updated += action == "updated"
                    reused += action == "reused"
                return ItemPublicationReceipt(len(values), created, updated, reused)
        except PublicationStateChanged:
            raise
        except Exception as exc:
            raise PostgresUnavailableError(
                f"문항 동기화에 실패해 전체 작업을 롤백했습니다: {_safe_error(exc, self.database_url)}"
            ) from exc

    def list_recent_sets(self, limit: int = 20) -> list[dict]:
        psycopg, _ = _psycopg()
        try:
            with psycopg.connect(self.database_url) as connection:
                cursor = connection.execute(
                    """SELECT s.set_id, s.section, s.generator_provider, s.generator_model,
                              s.generator_version, s.set_sequence,
                              s.review_status, s.published_at,
                              COUNT(i.position) AS item_count
                       FROM topik_bank.question_sets s
                       LEFT JOIN topik_bank.items i ON i.set_id = s.set_id
                       GROUP BY s.set_id, s.section, s.generator_provider, s.generator_model,
                                s.generator_version, s.set_sequence,
                                s.review_status, s.published_at
                       ORDER BY s.published_at DESC
                       LIMIT %s""",
                    (limit,),
                )
                rows = cursor.fetchall()
                return _dict_rows(cursor, rows)
        except Exception as exc:
            raise PostgresUnavailableError(
                f"발행 이력을 읽지 못했습니다: {_safe_error(exc, self.database_url)}"
            ) from exc

    def list_deployment_runs(self, limit: int = 50) -> list[dict]:
        """Read historical deployments without contacting their former targets."""
        return self._query_dicts(
            """SELECT r.run_id, r.target_label, r.status, r.requested_set_count,
                      r.transferred_set_count, r.reused_set_count,
                      r.created_row_count, r.reused_row_count, r.error_message,
                      r.started_at, r.completed_at, COUNT(s.set_id) AS audited_set_count
               FROM topik_bank.deployment_runs r
               LEFT JOIN topik_bank.deployment_run_sets s ON s.run_id = r.run_id
               GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT %s""",
            "과거 배포 이력을 읽지 못했습니다", (limit,),
        )

    def list_current_items(self) -> list[dict]:
        return self._query_dicts(
            """SELECT *,COALESCE((source_provenance->'publication_metadata'->>'predicted_difficulty')::double precision,0.0) AS predicted_difficulty
               FROM topik_bank.current_items ORDER BY section, generator_version, type_slot, source_key""",
            "최신 문항을 읽지 못했습니다",
        )

    def list_current_set_contents(self) -> list[dict]:
        return self._query_dicts(
            """SELECT * FROM topik_bank.current_set_contents
               ORDER BY set_section, set_generator_version, set_sequence, position""",
            "세트 구성을 읽지 못했습니다",
        )

    def list_all_set_memberships(self) -> list[dict]:
        return self._query_dicts(
            """SELECT s.set_id, s.set_sequence, s.section, s.generator_provider,
                      s.generator_model, s.generator_version, i.position, i.source_key
               FROM topik_bank.question_sets s
               JOIN topik_bank.items i ON i.set_id = s.set_id
               ORDER BY s.section, s.generator_version, s.set_sequence,
                        i.position""",
            "전체 세트 문항 사용 이력을 읽지 못했습니다",
        )

    def list_all_set_source_keys(self) -> set[str]:
        return {str(value["source_key"]) for value in self.list_all_set_memberships()}

    def list_current_set_source_keys(self) -> set[str]:
        """Backward-compatible alias; membership now covers all historical sets."""
        return self.list_all_set_source_keys()

    def next_set_sequence(
        self,
        section: str,
        backend: str,
        provider_key: str,
        model_id: str,
    ) -> int:
        rows = self._query_dicts(
            """SELECT COALESCE(MAX(set_sequence), 0) + 1 AS next_sequence
               FROM topik_bank.question_sets
               WHERE section = %s AND generator_provider = %s
                 AND generator_model = %s AND generator_version = %s""",
            "다음 세트 번호를 읽지 못했습니다",
            (section, backend, provider_key, model_id),
        )
        return int(rows[0]["next_sequence"])

    def _query_dicts(self, sql: str, error_prefix: str, params: tuple = ()) -> list[dict]:
        psycopg, _ = _psycopg()
        try:
            with psycopg.connect(self.database_url) as connection:
                cursor = connection.execute(sql, params)
                return _dict_rows(cursor, cursor.fetchall())
        except Exception as exc:
            raise PostgresUnavailableError(
                f"{error_prefix}: {_safe_error(exc, self.database_url)}"
            ) from exc




def _set_memberships_for_sources(connection, source_keys: list[str]) -> list[dict]:
    if not source_keys:
        return []
    cursor = connection.execute(
        """SELECT s.set_id, s.set_sequence, s.section, s.generator_provider,
                  s.generator_model, s.generator_version, i.position, i.source_key
           FROM topik_bank.question_sets s
           JOIN topik_bank.items i ON i.set_id = s.set_id
           WHERE i.source_key = ANY(%s)
           ORDER BY i.position""",
        (source_keys,),
    )
    return _dict_rows(cursor, cursor.fetchall())


def _resolve_set_membership(
    membership_rows: list[dict],
    requested_by_position: dict[int, str],
    requested_identity: tuple[str, str, str, str],
) -> tuple[tuple[str, int] | None, list[str]]:
    memberships: dict[tuple[str, int], dict[int, str]] = {}
    identities: dict[tuple[str, int], tuple[str, str, str, str]] = {}
    for row in membership_rows:
        membership_key = (
            str(row["set_id"]),
            int(row["set_sequence"]),
        )
        memberships.setdefault(membership_key, {})[int(row["position"])] = str(
            row["source_key"]
        )
        identities[membership_key] = (
            str(row["section"]),
            str(row["generator_provider"]),
            str(row["generator_model"]),
            str(row["generator_version"]),
        )
    for membership_key, membership in memberships.items():
        if identities[membership_key] == requested_identity and membership == requested_by_position:
            return membership_key, []
    return None, sorted({str(row["source_key"]) for row in membership_rows})


class PublicationStateChanged(ValueError):
    """Known pre-write conflict; this is not an uncertain commit outcome."""


def _invalidate_set_audio(connection, old):
    if not old["set_id"] or old["section"] != "listening":
        return
    if not connection.execute("SELECT to_regclass('topik_bank.question_set_item_audio_bindings')").fetchone()[0]:
        return
    # A shared track may include both questions; invalidate the whole affected group.
    rows = connection.execute("""SELECT audio_asset_id FROM topik_bank.question_set_item_audio_bindings
        WHERE set_id=%s AND position=%s AND is_current
        UNION SELECT audio_asset_id FROM topik_bank.item_audio_bindings
        WHERE item_id=%s AND content_hash=%s AND is_current""",
        (old["set_id"], old["position"], old["item_id"], old["content_hash"])).fetchall()
    assets = [r[0] for r in rows]
    if assets:
        connection.execute("""UPDATE topik_bank.question_set_item_audio_bindings SET is_current=false
            WHERE set_id=%s AND audio_asset_id=ANY(%s::uuid[])""", (old["set_id"], assets))
        connection.execute("""UPDATE topik_bank.item_audio_bindings b SET is_current=false
            FROM topik_bank.items i WHERE b.item_id=i.item_id AND i.set_id=%s AND b.audio_asset_id=ANY(%s::uuid[])""",
            (old["set_id"], assets))


def item_state(connection, source_keys, *, lock=False):
    cursor = connection.execute(
        "SELECT source_key,to_jsonb(i) FROM topik_bank.items i WHERE source_key=ANY(%s)" + (" FOR UPDATE" if lock else ""),
        (list(source_keys),),
    )
    rows = dict(cursor.fetchall())
    return {key: rows.get(key) for key in source_keys}


def _check_expected_state(connection, items, expected, validate_source):
    if expected is not None:
        keys = [i.candidate.source_key for i in items]
        if set(keys) != set(expected) or item_state(connection, keys, lock=True) != expected:
            raise PublicationStateChanged("확인 이후 DB 문항이 변경되었습니다. 새로고침 후 다시 선택하세요.")
    if validate_source is not None:
        try:
            validate_source()
        except ValueError as exc:
            raise PublicationStateChanged("확인 이후 문제·승인 상태가 변경되었습니다. 다시 선택하세요.") from exc


ITEM_CONTENT_COLUMNS = (
    "section", "type_slot", "item_type", "primary_skill", "target_level",
    "stem_length",
    "choice_count", "generator_provider", "generator_model", "generator_version",
    "prompt_version", "review_status", "stem", "choices", "correct_answer",
    "explanation", "content_json", "source_provenance", "content_hash",
)
ITEM_COLUMNS = ("item_id", "source_key", *ITEM_CONTENT_COLUMNS,
                "created_at", "updated_at", "set_id", "position")


def _upsert_item(connection, item: PublicationItemDraft, Jsonb, *, review_status="reviewed") -> tuple[Any, str]:
    candidate = item.candidate
    existing = connection.execute(
        "SELECT item_id, content_hash, source_provenance, position, review_status FROM topik_bank.items WHERE source_key=%s FOR UPDATE",
        (candidate.source_key,),
    ).fetchone()
    if existing and existing[3] is not None and existing[3] != candidate.slot:
        raise ValueError("세트에 소속된 문항의 번호는 동기화로 변경할 수 없습니다.")
    provenance = _source_provenance(candidate, item.metadata)
    if existing and existing[1] == item.content_hash and existing[2] == provenance and existing[4] == review_status:
        return existing[0], "reused"
    item_id = existing[0] if existing else candidate.item_id
    columns = (
        "section", "type_slot", "item_type", "primary_skill", "target_level",
        "stem_length", "choice_count", "generator_provider",
        "generator_model", "generator_version", "prompt_version", "review_status",
        "stem", "choices", "correct_answer", "explanation", "content_json",
        "source_provenance", "content_hash",
    )
    values = (
        candidate.section, candidate.slot, candidate.question_type,
        item.metadata.primary_skill.strip(), item.metadata.target_level,
        item.stem_length, item.choice_count,
        candidate.backend, candidate.provider_key, candidate.model_id,
        candidate.prompt_version, review_status, item.stem, Jsonb(item.choices),
        candidate.question.get("answer"), str(candidate.question.get("explanation", "")),
        Jsonb(candidate.question), Jsonb(provenance), item.content_hash,
    )
    if existing:
        assignments = ", ".join(f"{column}=%s" for column in columns)
        connection.execute(
            f"UPDATE topik_bank.items SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE item_id=%s",
            (*values, item_id),
        )
        return item_id, "updated"
    names = ", ".join(("item_id", "source_key", *columns))
    placeholders = ", ".join(["%s"] * (len(columns) + 2))
    connection.execute(
        f"INSERT INTO topik_bank.items({names}) VALUES ({placeholders})",
        (item_id, candidate.source_key, *values),
    )
    return item_id, "created"


def _source_provenance(candidate, metadata=None) -> dict[str, object]:
    return {
        **({"publication_metadata": {"predicted_difficulty": metadata.predicted_difficulty}} if metadata else {}),
        "source_key": candidate.source_key,
        "source_db": candidate.source_db,
        "generated_question_id": candidate.generated_id,
        "run_id": candidate.run_id,
        "created_at": candidate.created_at,
        "generation_preset": candidate.generation_preset,
        "request_parameters": candidate.request_parameters,
        **({"batch_id": candidate.batch_id} if candidate.batch_id else {}),
    }


def _dict_rows(cursor, rows) -> list[dict]:
    columns = [value.name for value in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def _insert_set_items(connection, rows: list[tuple]) -> None:
    for set_id, position, item_id in rows:
        cursor = connection.execute(
            """UPDATE topik_bank.items SET set_id=%s, position=%s
               WHERE item_id=%s AND set_id IS NULL""",
            (set_id, position, item_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("문항이 없거나 이미 다른 세트에 속해 있습니다.")


def _uses_unversioned_schema(connection) -> bool:
    return bool(connection.execute(
        """SELECT to_regclass('topik_bank.items') IS NOT NULL
           AND to_regclass('topik_bank.question_sets') IS NOT NULL
           AND to_regclass('topik_bank.item_versions') IS NULL
           AND to_regclass('topik_bank.question_set_items') IS NULL
           AND EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema='topik_bank' AND table_name='items' AND column_name='set_id')
           AND EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_schema='topik_bank' AND table_name='items' AND column_name='content_json')"""
    ).fetchone()[0])


def _uses_item_only_set_schema(connection) -> bool:
    """Return True for the schema introduced by Unigate-Web migration 016."""
    return bool(
        connection.execute(
            """SELECT
                   to_regclass('topik_bank.question_sets') IS NOT NULL
                   AND to_regclass('topik_bank.question_set_items') IS NOT NULL
                   AND to_regclass('topik_bank.question_set_versions') IS NULL
                   AND EXISTS (
                       SELECT 1 FROM information_schema.columns
                       WHERE table_schema='topik_bank' AND table_name='question_sets'
                         AND column_name='set_fingerprint'
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM information_schema.columns
                       WHERE table_schema='topik_bank' AND table_name='question_set_items'
                         AND column_name='set_version'
                   )"""
        ).fetchone()[0]
    )


def _has_external_set_version_dependencies(connection) -> bool:
    if not connection.execute(
        "SELECT to_regclass('topik_bank.question_set_versions') IS NOT NULL"
    ).fetchone()[0]:
        return False
    return bool(
        connection.execute(
            """SELECT EXISTS (
                   SELECT 1
                     FROM pg_constraint constraint_row
                     JOIN pg_class relation ON relation.oid=constraint_row.conrelid
                     JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
                    WHERE constraint_row.confrelid='topik_bank.question_set_versions'::regclass
                      AND namespace.nspname<>'topik_bank'
               )"""
        ).fetchone()[0]
    )


def _psycopg():
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise PostgresUnavailableError(
            "PostgreSQL 드라이버가 없습니다. requirements.txt 의존성을 다시 설치하세요."
        ) from exc
    return psycopg, Jsonb


def _safe_error(exc: Exception, database_url: str) -> str:
    message = str(exc).replace(database_url, "[DATABASE_URL]")
    try:
        password = urlsplit(database_url).password
    except ValueError:
        password = None
    if password:
        message = message.replace(password, "***")
    return message
