"""Explicit, backed-up conversion of local banks to item audio bindings."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import uuid

from dotenv import dotenv_values
import psycopg
from psycopg.conninfo import conninfo_to_dict

from topik_question_lab.maintenance import bank_server
from topik_question_lab.maintenance.bank_migrations import _backup_database, StartupMigrationError
from topik_question_lab.common.question_bank.audio_bindings import require_item_audio_schema
from topik_question_lab.common.question_bank.postgres_storage import MIGRATION_DIR

VERSION = "011_item_audio_only"
PRESERVED = ("items", "question_sets", "tts_audio_assets", "item_visual_assets")


def _rows(c, table):
    return [r[0] for r in c.execute(f"SELECT to_jsonb(t) FROM topik_bank.{table} t")]


def _digest(rows):
    serialized = sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)
    return hashlib.sha256("\n".join(serialized).encode()).hexdigest()


def migration_baseline(c):
    preserved = {t: _rows(c, t) for t in PRESERVED}
    items = {r["item_id"]: r for r in preserved["items"]}
    members = {(r["set_id"], r["position"]): r for r in items.values() if r["set_id"]}
    assets = {r["audio_asset_id"]: r for r in preserved["tts_audio_assets"]}
    old_items, old_sets = _rows(c, "item_audio_bindings"), _rows(c, "question_set_item_audio_bindings")
    expected, active = [], set()
    for row in old_sets:
        item = members.get((row["set_id"], row["position"]))
        if item is None:
            raise ValueError("문항을 찾을 수 없는 세트 음원 연결이 있습니다.")
        current = row["is_current"] and assets[row["audio_asset_id"]]["deleted_at"] is None
        key = f"set_audio:{row['set_id']}:{row['position']}:{row['audio_asset_id']}"
        expected.append(dict(binding_id=str(uuid.UUID(hashlib.md5(key.encode()).hexdigest())),
            item_id=item["item_id"], content_hash=item["content_hash"] if current else None,
            audio_asset_id=row["audio_asset_id"], source_hash=row["source_hash"],
            is_current=current, created_at=row["created_at"], origin_set_id=row["set_id"], origin_position=row["position"]))
        if current:
            active.add(item["item_id"])
    for row in old_items:
        item = items[row["item_id"]]
        current = (row["is_current"] and row["content_hash"] == item["content_hash"]
                   and assets[row["audio_asset_id"]]["deleted_at"] is None and row["item_id"] not in active)
        key = f"item_audio:{row['item_id']}:{row['content_hash']}:{row['audio_asset_id']}"
        expected.append({**row, "binding_id": str(uuid.UUID(hashlib.md5(key.encode()).hexdigest())),
                         "is_current": current, "origin_set_id": None, "origin_position": None})
    return {"preserved": {t: (len(r), _digest(r)) for t, r in preserved.items()},
            "bindings": (len(expected), _digest(expected)),
            "current": sum(r["is_current"] for r in expected),
            "old_item_bindings": len(old_items), "old_set_bindings": len(old_sets)}


def verify_migration(c, baseline):
    require_item_audio_schema(c)
    for table, expected in baseline["preserved"].items():
        rows = _rows(c, table)
        if (len(rows), _digest(rows)) != expected:
            raise ValueError("기존 문항·세트·미디어 보존 검증에 실패했습니다.")
    rows = _rows(c, "item_audio_bindings")
    if (len(rows), _digest(rows)) != baseline["bindings"]:
        raise ValueError("음원 연결 이력 또는 현재 연결 보존 검증에 실패했습니다.")
    return {"items": baseline["preserved"]["items"][0], "audio_assets": baseline["preserved"]["tts_audio_assets"][0],
            "bindings": len(rows), "current_bindings": baseline["current"],
            "old_item_bindings": baseline["old_item_bindings"], "old_set_bindings": baseline["old_set_bindings"]}


def migrate_item_audio(root: Path, url: str, config: dict):
    options = conninfo_to_dict(url)
    if options.get("host") not in ("localhost", "127.0.0.1", "::1") or options.get("service") or (
        options.get("hostaddr") and options["hostaddr"] not in ("127.0.0.1", "::1")):
        raise ValueError("명시적인 로컬 PostgreSQL만 이관할 수 있습니다.")
    backup = None
    try:
        with psycopg.connect(url, connect_timeout=8, options="-c timezone=UTC -c lock_timeout=15000 -c statement_timeout=120000") as c:
            for lock in ("schema_migrations", "production_deployment", "set_membership", "item_sync"):
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("topik_bank:" + lock,))
            if c.execute("SELECT to_regclass('topik_bank.question_set_item_audio_bindings') IS NULL").fetchone()[0]:
                require_item_audio_schema(c)
                return {"already_migrated": True}
            c.execute("""LOCK TABLE topik_bank.items,topik_bank.question_sets,topik_bank.tts_audio_assets,
                topik_bank.item_visual_assets,topik_bank.item_audio_bindings,topik_bank.question_set_item_audio_bindings
                IN SHARE ROW EXCLUSIVE MODE""")
            baseline = migration_baseline(c)
            snapshot = c.execute("SELECT pg_export_snapshot()").fetchone()[0]
            backup = _backup_database(root, url, config, label="011", snapshot=snapshot)
            c.execute((MIGRATION_DIR / (VERSION + ".sql")).read_text(encoding="utf-8"))
            report = verify_migration(c, baseline)
            if c.execute("SELECT to_regclass('topik_bank.schema_migrations') IS NOT NULL").fetchone()[0]:
                c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING", (VERSION,))
        with psycopg.connect(url, connect_timeout=8, options="-c timezone=UTC -c default_transaction_read_only=on") as c:
            verify_migration(c, baseline)
        return {**report, "backup": str(backup), "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest()}
    except Exception:
        saved = f" 백업: {backup}" if backup else ""
        raise StartupMigrationError("문항 음원 이관을 완료하지 못했습니다. 현재 구조와 검증 결과를 다시 확인하세요." + saved) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-env", choices=("DATABASE_URL", "QUESTION_BANK_LOCAL_DATABASE_URL"), required=True)
    args = parser.parse_args()
    url = dotenv_values(bank_server.ROOT / ".env").get(args.database_env)
    if not url:
        raise SystemExit("선택한 로컬 DB 설정이 없습니다.")
    try:
        report = migrate_item_audio(bank_server.ROOT, url, bank_server.read_config())
    except (ValueError, StartupMigrationError):
        raise SystemExit("이관을 완료하지 못했습니다. 로컬 DB 상태와 백업을 확인하세요.") from None
    print(json.dumps({"target": args.database_env, **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
