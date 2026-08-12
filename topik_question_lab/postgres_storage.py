from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .publication import PublicationItemDraft, PublicationSetDraft, set_fingerprint, validate_complete_selection


MIGRATION_DIR = Path(__file__).resolve().parent / "migrations"


class PostgresUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublicationReceipt:
    set_id: str
    set_version: int
    set_sequence: int
    created_item_versions: int
    reused_item_versions: int
    created_set_version: bool


@dataclass(frozen=True)
class ItemPublicationReceipt:
    total_items: int
    created_item_versions: int
    reused_item_versions: int


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
                for path in sorted(MIGRATION_DIR.glob("*.sql")):
                    version = path.stem
                    exists = connection.execute(
                        "SELECT 1 FROM topik_bank.schema_migrations WHERE version = %s", (version,)
                    ).fetchone()
                    if exists:
                        continue
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

    def publish_set(self, draft: PublicationSetDraft) -> PublicationReceipt:
        validate_complete_selection(item.candidate for item in draft.items)
        psycopg, Jsonb = _psycopg()
        try:
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('topik_bank:set_membership'))"
                )
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"{draft.section}:{draft.backend}:{draft.provider_key}:{draft.model_id}",),
                )
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
                    set_id, set_sequence, set_version = existing_membership
                    return PublicationReceipt(
                        set_id=set_id,
                        set_version=set_version,
                        set_sequence=set_sequence,
                        created_item_versions=0,
                        reused_item_versions=len(draft.items),
                        created_set_version=False,
                    )
                if conflicts:
                    preview = ", ".join(conflicts[:8])
                    suffix = f" 외 {len(conflicts) - 8}개" if len(conflicts) > 8 else ""
                    raise ValueError(
                        "이미 PostgreSQL 세트에 사용된 문항은 새 세트에 포함할 수 없습니다: "
                        f"{preview}{suffix}"
                    )

                item_versions: list[tuple[Any, int]] = []
                created_items = 0
                reused_items = 0
                for item in draft.items:
                    item_id, item_version, created = _upsert_item_version(connection, item, Jsonb)
                    created_items += 1 if created else 0
                    reused_items += 0 if created else 1
                    item_versions.append((item_id, item_version))

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
                           generator_version, set_sequence
                       ) VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        set_id,
                        draft.section,
                        draft.backend,
                        draft.provider_key,
                        draft.model_id,
                        set_sequence,
                    ),
                )
                fingerprint = set_fingerprint(
                    item_versions, draft.default_target_level, draft.default_predicted_difficulty
                )
                set_version = 1
                connection.execute(
                    """INSERT INTO topik_bank.question_set_versions(
                           set_id, set_version, review_status, default_target_level,
                           default_predicted_difficulty, set_fingerprint
                       ) VALUES (%s, %s, 'reviewed', %s, %s, %s)""",
                    (
                        set_id,
                        set_version,
                        draft.default_target_level,
                        draft.default_predicted_difficulty,
                        fingerprint,
                    ),
                )
                _insert_set_items(
                    connection,
                    [
                        (set_id, set_version, position, item_id, item_version)
                        for position, (item_id, item_version) in enumerate(item_versions, start=1)
                    ],
                )
                return PublicationReceipt(
                    set_id=str(set_id),
                    set_version=set_version,
                    set_sequence=set_sequence,
                    created_item_versions=created_items,
                    reused_item_versions=reused_items,
                    created_set_version=True,
                )
        except PostgresUnavailableError:
            raise
        except ValueError:
            raise
        except Exception as exc:
            raise PostgresUnavailableError(
                f"세트 발행에 실패해 전체 작업을 롤백했습니다: {_safe_error(exc, self.database_url)}"
            ) from exc

    def publish_items(self, items: tuple[PublicationItemDraft, ...] | list[PublicationItemDraft]) -> ItemPublicationReceipt:
        values = tuple(items)
        if not values:
            return ItemPublicationReceipt(0, 0, 0)
        psycopg, Jsonb = _psycopg()
        try:
            with psycopg.connect(self.database_url) as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:item_sync'))")
                created = 0
                reused = 0
                for item in values:
                    _, _, was_created = _upsert_item_version(connection, item, Jsonb)
                    created += 1 if was_created else 0
                    reused += 0 if was_created else 1
                return ItemPublicationReceipt(len(values), created, reused)
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
                              s.generator_version, s.set_sequence, v.set_version,
                              v.review_status, v.published_at,
                              COUNT(i.position) AS item_count
                       FROM topik_bank.question_sets s
                       JOIN topik_bank.question_set_versions v ON v.set_id = s.set_id
                       JOIN topik_bank.question_set_items i
                         ON i.set_id = v.set_id AND i.set_version = v.set_version
                       GROUP BY s.set_id, s.section, s.generator_provider, s.generator_model,
                                s.generator_version, s.set_sequence, v.set_version,
                                v.review_status, v.published_at
                       ORDER BY v.published_at DESC
                       LIMIT %s""",
                    (limit,),
                )
                rows = cursor.fetchall()
                return _dict_rows(cursor, rows)
        except Exception as exc:
            raise PostgresUnavailableError(
                f"발행 이력을 읽지 못했습니다: {_safe_error(exc, self.database_url)}"
            ) from exc

    def list_current_items(self) -> list[dict]:
        return self._query_dicts(
            """SELECT * FROM topik_bank.current_items
               ORDER BY section, generator_version, type_slot, source_key""",
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
                      s.generator_model, s.generator_version, si.set_version,
                      si.position, i.source_key
               FROM topik_bank.question_sets s
               JOIN topik_bank.question_set_items si ON si.set_id = s.set_id
               JOIN topik_bank.items i ON i.item_id = si.item_id
               ORDER BY s.section, s.generator_version, s.set_sequence,
                        si.set_version, si.position""",
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


def migration_checksums() -> dict[str, str]:
    return {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(MIGRATION_DIR.glob("*.sql"))
    }


def _set_memberships_for_sources(connection, source_keys: list[str]) -> list[dict]:
    if not source_keys:
        return []
    cursor = connection.execute(
        """SELECT s.set_id, s.set_sequence, s.section, s.generator_provider,
                  s.generator_model, s.generator_version, si.set_version,
                  si.position, i.source_key
           FROM topik_bank.question_sets s
           JOIN topik_bank.question_set_items si ON si.set_id = s.set_id
           JOIN topik_bank.items i ON i.item_id = si.item_id
           WHERE i.source_key = ANY(%s)
           ORDER BY si.set_version DESC, si.position""",
        (source_keys,),
    )
    return _dict_rows(cursor, cursor.fetchall())


def _resolve_set_membership(
    membership_rows: list[dict],
    requested_by_position: dict[int, str],
    requested_identity: tuple[str, str, str, str],
) -> tuple[tuple[str, int, int] | None, list[str]]:
    memberships: dict[tuple[str, int, int], dict[int, str]] = {}
    identities: dict[tuple[str, int, int], tuple[str, str, str, str]] = {}
    for row in membership_rows:
        membership_key = (
            str(row["set_id"]),
            int(row["set_sequence"]),
            int(row["set_version"]),
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


def _upsert_item_version(connection, item: PublicationItemDraft, Jsonb) -> tuple[Any, int, bool]:
    candidate = item.candidate
    connection.execute(
        """INSERT INTO topik_bank.items(item_id, source_key)
           VALUES (%s, %s)
           ON CONFLICT (source_key) DO NOTHING""",
        (candidate.item_id, candidate.source_key),
    )
    identity = connection.execute(
        "SELECT item_id FROM topik_bank.items WHERE source_key = %s",
        (candidate.source_key,),
    ).fetchone()
    item_id = identity[0]
    existing = connection.execute(
        """SELECT item_version FROM topik_bank.item_versions
           WHERE item_id = %s AND content_hash = %s""",
        (item_id, item.content_hash),
    ).fetchone()
    if existing:
        return item_id, int(existing[0]), False
    item_version = int(
        connection.execute(
            "SELECT COALESCE(MAX(item_version), 0) + 1 FROM topik_bank.item_versions WHERE item_id = %s",
            (item_id,),
        ).fetchone()[0]
    )
    provenance = _source_provenance(candidate)
    connection.execute(
        """INSERT INTO topik_bank.item_versions(
               item_id, item_version, section, type_slot, item_type, primary_skill,
               target_level, predicted_difficulty, irt_difficulty, irt_discrimination,
               stem_length, choice_count, generator_provider, generator_model,
               generator_version, prompt_version, review_status, stem, choices,
               correct_answer, explanation, content_json, source_provenance, content_hash
           ) VALUES (
               %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s, %s, %s,
               %s, %s, 'reviewed', %s, %s, %s, %s, %s, %s, %s
           )""",
        (
            item_id,
            item_version,
            candidate.section,
            candidate.slot,
            candidate.question_type,
            item.metadata.primary_skill.strip(),
            item.metadata.target_level,
            item.metadata.predicted_difficulty,
            item.stem_length,
            item.choice_count,
            candidate.backend,
            candidate.provider_key,
            candidate.model_id,
            candidate.prompt_version,
            item.stem,
            Jsonb(item.choices),
            candidate.question.get("answer"),
            str(candidate.question.get("explanation", "")),
            Jsonb(candidate.question),
            Jsonb(provenance),
            item.content_hash,
        ),
    )
    return item_id, item_version, True


def _source_provenance(candidate) -> dict[str, object]:
    return {
        "source_key": candidate.source_key,
        "source_db": candidate.source_db,
        "generated_question_id": candidate.generated_id,
        "run_id": candidate.run_id,
        "created_at": candidate.created_at,
        "generation_preset": candidate.generation_preset,
        "request_parameters": candidate.request_parameters,
    }


def _dict_rows(cursor, rows) -> list[dict]:
    columns = [value.name for value in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def _insert_set_items(connection, rows: list[tuple]) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO topik_bank.question_set_items(
                   set_id, set_version, position, item_id, item_version
               ) VALUES (%s, %s, %s, %s, %s)""",
            rows,
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
