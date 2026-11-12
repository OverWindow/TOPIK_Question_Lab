"""Validate and import the TOPIK II vocabulary CSV into the managed local bank."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path

from dotenv import load_dotenv
import psycopg

from topik_question_lab.maintenance import bank_server
from topik_question_lab.maintenance.bank_migrations import _backup_database, _local_config
from topik_question_lab.common.question_bank.postgres_storage import MIGRATION_DIR
from topik_question_lab.common.vocabulary.vocabulary_languages import LANGUAGE_FIELDS, apply_language_migration


VERSION = "012_vocabulary"
HEADERS = (
    "Korean", "Part of speech", "English", "Vietnamese", "Japanese",
    "Example (Korean)", "Example (English)", "TOPIK level",
)
COLUMNS = "korean, part_of_speech, english, vietnamese, japanese, example_korean, example_english, topik_level"
EXTENDED_HEADERS = HEADERS + ("Chinese (Simplified)", "Indonesian")
EXTENDED_COLUMNS = COLUMNS + ", " + ", ".join(LANGUAGE_FIELDS)


class VocabularyImportError(RuntimeError):
    pass


def read_vocabulary(path: Path) -> list[tuple]:
    rows, keys = [], set()
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            headers = tuple(next(reader, ()))
            if headers not in (HEADERS, EXTENDED_HEADERS):
                raise ValueError("CSV 헤더가 단어장 8개 또는 10개 열과 일치하지 않습니다.")
            for values in reader:
                line = reader.line_num
                if len(values) != len(headers):
                    raise ValueError(f"CSV {line}행의 열 개수가 올바르지 않습니다.")
                if any("\x00" in value for value in values):
                    raise ValueError(f"CSV {line}행에 저장할 수 없는 NUL 문자가 있습니다.")
                if any(not value.strip() for i, value in enumerate(values[:8]) if i != 1):
                    raise ValueError(f"CSV {line}행의 필수 값이 비어 있습니다.")
                if values[7] not in {"3", "4", "5", "6"}:
                    raise ValueError(f"CSV {line}행의 TOPIK 급수는 3~6이어야 합니다.")
                key = tuple(values[:2])
                if key in keys:
                    raise ValueError(f"CSV {line}행에 중복된 단어·품사가 있습니다.")
                keys.add(key)
                rows.append((*values[:7], int(values[7]), *values[8:]))
    except (OSError, UnicodeError, csv.Error):
        raise ValueError("UTF-8 단어장 CSV 파일을 읽을 수 없습니다.") from None
    if not rows:
        raise ValueError("CSV에 등록할 단어가 없습니다.")
    return rows


def _stored_rows(connection, columns=COLUMNS) -> dict[tuple, tuple]:
    return {row[:2]: row for row in connection.execute(f"SELECT {columns} FROM topik_bank.vocabulary")}


def _upsert_vocabulary(connection, rows: list[tuple]) -> dict:
    extended = len(rows[0]) == len(EXTENDED_HEADERS)
    columns = EXTENDED_COLUMNS if extended else COLUMNS
    previous = _stored_rows(connection, columns) if extended else _stored_rows(connection)
    if extended:
        # Missing translations must not erase meanings already reviewed in the bank.
        rows = [(*row[:8], *(value if value.strip() else previous.get(row[:2], ("",) * 10)[i]
                            for i, value in enumerate(row[8:], 8))) for row in rows]
    created = sum(row[:2] not in previous for row in rows)
    unchanged = sum(previous.get(row[:2]) == row for row in rows)
    fields = columns.split(", ")
    updates = ", ".join(f"{field} = EXCLUDED.{field}" for field in fields[2:])
    old_values = ", ".join(f"v.{field}" for field in fields[2:])
    new_values = ", ".join(f"EXCLUDED.{field}" for field in fields[2:])
    with connection.cursor() as cursor:
        cursor.executemany(
            f"""INSERT INTO topik_bank.vocabulary AS v ({columns})
                VALUES ({', '.join(['%s'] * len(fields))})
                ON CONFLICT (korean, part_of_speech) DO UPDATE SET {updates}
                WHERE ({old_values}) IS DISTINCT FROM ({new_values})""",
            rows,
        )
    expected = {**previous, **{row[:2]: row for row in rows}}
    stored = _stored_rows(connection, columns) if extended else _stored_rows(connection)
    if stored != expected:
        raise ValueError("등록 후 단어장 전체 내용 검증에 실패했습니다.")
    return {
        "csv_rows": len(rows), "created": created,
        "updated": len(rows) - created - unchanged, "unchanged": unchanged,
        "total_rows": len(expected),
        "levels": dict(sorted(Counter(row[7] for row in rows).items())),
        "empty_part_of_speech": sum(not row[1] for row in rows),
    }


def import_vocabulary(root: Path, url: str, csv_path: Path) -> dict:
    rows = read_vocabulary(csv_path)
    config = _local_config(root, url)
    backup = None
    committed = False
    try:
        with psycopg.connect(url, connect_timeout=8,
                             options="-c lock_timeout=15000 -c statement_timeout=120000") as connection:
            actual = Path(connection.execute("SHOW data_directory").fetchone()[0]).resolve()
            if actual != Path(config["data_directory"]).resolve():
                raise ValueError("관리 대상 로컬 Bank와 다른 서버입니다.")
            for lock in ("schema_migrations", "vocabulary_import"):
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("topik_bank:" + lock,))
            applied = connection.execute(
                "SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s", (VERSION,)
            ).fetchone()
            if applied:
                connection.execute("LOCK TABLE topik_bank.vocabulary IN SHARE ROW EXCLUSIVE MODE")
            snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
            backup = _backup_database(root, url, config, label="vocabulary", snapshot=snapshot)
            if not applied:
                connection.execute((MIGRATION_DIR / f"{VERSION}.sql").read_text(encoding="utf-8"))
                connection.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (VERSION,))
            if len(rows[0]) == len(EXTENDED_HEADERS):
                apply_language_migration(connection)
            report = _upsert_vocabulary(connection, rows)
        committed = True
        with psycopg.connect(url, connect_timeout=8,
                             options="-c default_transaction_read_only=on -c statement_timeout=15000") as connection:
            extended = len(rows[0]) == len(EXTENDED_HEADERS)
            stored = _stored_rows(connection, EXTENDED_COLUMNS) if extended else _stored_rows(connection)
            if any(row[:2] not in stored or stored[row[:2]][:8] != row[:8]
                   or any(value.strip() and stored[row[:2]][i] != value
                          for i, value in enumerate(row[8:], 8)) for row in rows):
                raise ValueError("커밋 후 CSV 대조에 실패했습니다.")
        return {**report, "verified": True, "backup": str(backup)}
    except Exception:
        message = ("등록은 커밋되었지만 최종 조회 검증에 실패했습니다." if committed
                   else "단어장 등록을 완료하지 못했습니다. 로컬 DB 상태를 확인하세요.")
        if backup:
            message += f" 백업: {backup}"
        raise VocabularyImportError(message) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=bank_server.ROOT / "TOPIK-II-Word" / "topik-2-all.csv")
    parser.add_argument("--check-only", action="store_true", help="Validate the CSV without connecting to PostgreSQL")
    args = parser.parse_args(argv)
    try:
        if args.check_only:
            rows = read_vocabulary(args.csv)
            report = {"csv_rows": len(rows), "levels": dict(sorted(Counter(row[7] for row in rows).items())),
                      "empty_part_of_speech": sum(not row[1] for row in rows)}
        else:
            load_dotenv(bank_server.ROOT / ".env")
            config = bank_server.read_config()
            url = bank_server.database_url(config)
            report = import_vocabulary(bank_server.ROOT, url, args.csv)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (ValueError, VocabularyImportError) as exc:
        print(str(exc))
        return 1
    except Exception:
        print("단어장 등록을 완료하지 못했습니다. 로컬 DB 설정과 백업을 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
