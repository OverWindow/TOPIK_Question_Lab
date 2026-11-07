"""Read-only inspection of the local question bank."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from topik_question_lab.step_06_publication.postgres_storage import _psycopg, _uses_unversioned_schema, ITEM_COLUMNS
from topik_question_lab.step_06_publication.media_validation import MEDIA_TABLES, ASSET_TABLES


@dataclass(frozen=True)
class DatabaseStatus:
    configured: bool
    reachable: bool
    label: str
    database_name: str = ""
    migrations: tuple[str, ...] = ()
    item_count: int = 0
    set_count: int = 0
    ready: bool = False
    schema_model: str = ""
    message: str = ""


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


def inspect_database(database_url: str) -> DatabaseStatus:
    label = database_label(database_url)
    if not database_url.strip():
        return DatabaseStatus(False, False, label, message="연결 문자열이 설정되지 않았습니다.")
    psycopg, _ = _psycopg()
    try:
        with psycopg.connect(
            database_url, connect_timeout=8, prepare_threshold=None,
            options="-c default_transaction_read_only=on -c statement_timeout=15000"
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
                for table in ("items", "question_sets")
            }
            current_schema = _uses_unversioned_schema(connection)
            structure_problems = _required_structure_problems(connection)
            readiness_messages = []
            if not current_schema:
                readiness_messages.append("문항 버전 제거 마이그레이션(006)을 적용해야 합니다.")
            if structure_problems:
                readiness_messages.append("구조 확인 필요: " + ", ".join(structure_problems))
            return DatabaseStatus(
                configured=True,
                reachable=True,
                label=label,
                database_name=database_name,
                migrations=migrations,
                item_count=counts["items"],
                set_count=counts["question_sets"],
                ready=current_schema and not structure_problems,
                schema_model="unversioned" if current_schema else "migration_required",
                message=(" / ".join(readiness_messages) if readiness_messages else "스키마가 최신입니다."),
            )
    except Exception as exc:
        return DatabaseStatus(
            True, False, label,
            message=f"연결 실패: {_safe_error(exc, database_url)}",
        )


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


def _table_count(connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    allowed = {"items", "question_sets"}
    if table not in allowed:
        raise ValueError(f"허용되지 않은 테이블: {table}")
    return int(connection.execute(f"SELECT COUNT(*) FROM topik_bank.{table}").fetchone()[0])


def _required_structure_problems(connection) -> list[str]:
    problems: list[str] = []
    required_tables = {
        "items", "question_sets", *MEDIA_TABLES,
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
        *(("items", column) for column in ITEM_COLUMNS),
        *((table,column) for table in ASSET_TABLES for column in ("storage_backend","file_sha256")),
        ("question_sets", "set_sequence"),
        ("question_sets", "exam_format"),
        ("question_sets", "review_status"),
        ("question_sets", "default_target_level"),
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
    for table in ("item_versions", "question_set_items", "question_set_versions"):
        if table in existing_tables:
            problems.append(f"제거되지 않은 테이블 {table}")
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
                   WHERE conname = 'question_sets_exam_sequence_key'
                     AND conrelid = 'topik_bank.question_sets'::regclass
               )"""
        ).fetchone()[0]
    ) if "question_sets" in existing_tables else False
    if not has_sequence_constraint:
        problems.append("제약 question_sets_exam_sequence_key (마이그레이션 010 필요)")
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


def _hash_value(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
