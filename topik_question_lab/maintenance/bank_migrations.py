"""Back up and apply the reviewed 009 -> 010 upgrade on the managed local bank."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from topik_question_lab.maintenance import bank_server
from topik_question_lab.common.question_bank.postgres_storage import MIGRATION_DIR, PostgresQuestionBank
from topik_question_lab.common.media.media_registration import require_media_schema


AUTO_VERSION = "010_exam_format"


class StartupMigrationError(RuntimeError):
    pass


def _local_config(root: Path, url: str):
    if root.resolve() != bank_server.ROOT.resolve():
        raise StartupMigrationError("자동 마이그레이션은 이 저장소의 로컬 Bank에서만 실행할 수 있습니다.")
    config = bank_server.read_config()
    if url != bank_server.database_url(config):
        raise StartupMigrationError("자동 마이그레이션 대상이 로컬 Bank 설정과 다릅니다.")
    return config


def _backup_database(root: Path, url: str, config: dict, *, label="010", snapshot=None) -> Path:
    directory = root / "backups" / "postgres"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"before_{label}_{stamp}_{uuid.uuid4().hex[:8]}.dump"
    binaries = Path(config["bin_directory"])
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PG", "AWS_", "CHATKHU_", "DEEPSEEK_", "BIZROUTER_")) and "DATABASE_URL" not in k}
    # Credentials stay out of process arguments and error messages.
    params = conninfo_to_dict(url)
    password = params.pop("password", None)
    if password is not None:
        env["PGPASSWORD"] = password
    env["PGCONNECT_TIMEOUT"] = "8"
    try:
        for arguments in (
            [str(binaries / "pg_dump.exe"), "--dbname", make_conninfo(**params),
             "--format=custom", "--no-password", "--file", str(path),
             *(["--snapshot", snapshot] if snapshot else [])],
            [str(binaries / "pg_restore.exe"), "--list", str(path)],
        ):
            result = subprocess.run(arguments, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise StartupMigrationError("PostgreSQL 백업 또는 백업 파일 검증에 실패했습니다. DB는 변경하지 않았습니다.")
        if not path.is_file() or not path.stat().st_size:
            raise StartupMigrationError("PostgreSQL 백업 파일이 비어 있습니다. DB는 변경하지 않았습니다.")
    except (OSError, subprocess.TimeoutExpired):
        raise StartupMigrationError("PostgreSQL 백업 실행에 실패했습니다. DB는 변경하지 않았습니다.") from None
    return path


def migrate_local_bank(root: Path, url: str) -> tuple[list[str], Path | None]:
    if not url.strip():
        return [], None
    backup = None
    try:
        config = _local_config(root, url)
        with psycopg.connect(url, connect_timeout=8,
                             options="-c lock_timeout=15000 -c statement_timeout=60000") as c:
            actual = Path(c.execute("SHOW data_directory").fetchone()[0]).resolve()
            if actual != Path(config["data_directory"]).resolve():
                raise StartupMigrationError("접속한 서버가 관리 대상 로컬 Bank와 다릅니다.")
            c.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:schema_migrations'))")
            if c.execute("SELECT to_regclass('topik_bank.schema_migrations')").fetchone()[0] is None:
                raise StartupMigrationError("자동 적용에는 009까지 준비된 로컬 DB가 필요합니다. 먼저 수동 DB 구조 준비를 실행하세요.")
            applied = {row[0] for row in c.execute("SELECT version FROM topik_bank.schema_migrations").fetchall()}
            paths = sorted(MIGRATION_DIR.glob("*.sql"))
            pending = [path.stem for path in paths if path.stem not in applied]
            if "011_item_audio_only" in pending:
                raise StartupMigrationError("문항 음원 통합(011)은 백업·검증 후 수동 적용해야 합니다. 자동 변경하지 않았습니다.")
            if pending == ["017_vocabulary_questions"]:
                from topik_question_lab.common.vocabulary.vocabulary_questions import apply_question_migration
                backup = _backup_database(root, url, config, label="017")
                apply_question_migration(c)
                return pending, backup
            if pending and pending != [AUTO_VERSION]:
                raise StartupMigrationError("자동 적용 범위는 009 → 010입니다. 다른 미적용 마이그레이션은 수동 검토가 필요합니다.")
            if not pending:
                require_media_schema(c)
                return [], None
            if not any(version.startswith("009_") for version in applied):
                raise StartupMigrationError("자동 적용 전에 마이그레이션 009가 필요합니다.")
            backup = _backup_database(root, url, config)
            versions = PostgresQuestionBank(url)._apply_migrations(c)
            require_media_schema(c)
        return versions, backup
    except StartupMigrationError:
        raise
    except bank_server.BankServerError as exc:
        raise StartupMigrationError(str(exc)) from None
    except Exception as exc:
        # Driver errors can contain credentials or question contents.
        detail = f" (SQLSTATE {exc.sqlstate})" if isinstance(exc, psycopg.Error) and exc.sqlstate else ""
        saved = f" 백업: {backup}" if backup else ""
        raise StartupMigrationError(
            f"로컬 DB 자동 준비에 실패했습니다{detail}. 연결·권한·스키마를 확인하고 다시 시도하세요.{saved}"
        ) from None
