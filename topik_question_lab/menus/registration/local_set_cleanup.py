"""Explicit, backed-up SQLite cleanup; PostgreSQL connections are read-only."""
from topik_question_lab.common.generation.exam_profiles import question_count
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from topik_question_lab.common.local_cleanup import PREFIX, cleanup_receipts
from topik_question_lab.maintenance.backup_service import (
    BackupOptions, _create_backup, inspect_backup,
)
from topik_question_lab.common.generation.bulk_generation import execution_lock
from topik_question_lab.common.question_bank.direct_publication import DirectPublicationService, PublicationBlocked, _hash
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog
from topik_question_lab.common.question_bank.storage_overview import load_overview, bank_coverage, visual_requirements


APPROVED = {"reviewed", "pilot", "active"}


def _token(value):
    return _hash(json.loads(json.dumps(value, default=str, sort_keys=True)))


def _cloud_asset(asset):
    return (asset.get("storage_backend") == "s3"
            and str(asset.get("storage_url", "")).startswith(("https://", "http://"))
            and len(asset.get("file_sha256") or "") == 64)


class LocalSetCleanup:
    def __init__(self, root, url):
        self.root = Path(root).resolve()
        self.reader = DirectPublicationService(self.root, url, readonly=True)

    def candidates(self, snapshot=None, catalog=None):
        snapshot = snapshot or load_overview(self.root, self.reader.target_url)
        catalog = catalog or collect_candidate_catalog(self.root, include_trashed=True)
        bank = snapshot["bank"]
        if not bank["available"] or not bank["media_available"] or snapshot["errors"] or catalog.read_errors:
            raise PublicationBlocked("DB 연결·미디어 구조·로컬 기록을 모두 확인해야 정리할 수 있습니다.")
        receipts = cleanup_receipts(self.root)
        local = {r.candidate.source_key: r for r in catalog.records}
        result = []
        for s in bank["sets"]:
            sid = str(s["set_id"])
            items = sorted([i for i in bank["items"] if str(i["set_id"]) == sid], key=lambda i: i["position"] or 0)
            archived = {key for r in receipts if r["set_id"] == sid and r["target"] == self.reader.target for key in r["source_keys"]}
            records = [local[i["source_key"]] for i in items if i["source_key"] in local]
            reasons = []
            expected_count = question_count(s.get("exam_format", "pbt"))
            if len(items) != expected_count or [i["position"] for i in items] != list(range(1, expected_count + 1)):
                reasons.append(f"1~{expected_count}번 문항 등록 확인 필요")
            if s["review_status"] not in APPROVED or any(i["review_status"] not in APPROVED for i in items):
                reasons.append("PostgreSQL 세트·전체 문항 승인 필요")
            if any(not r.candidate.review.get("approved") or r.trashed_at for r in records):
                reasons.append("로컬 문제 승인·휴지통 상태 확인 필요")
            if any(str(local[i["source_key"]].candidate.item_id) != str(i["item_id"])
                   for i in items if i["source_key"] in local):
                reasons.append("문항 식별자 불일치")
            if any(i["source_key"] not in local and i["source_key"] not in archived for i in items):
                reasons.append("로컬 원본 또는 정리 이력 확인 필요")
            coverage = bank_coverage(items)
            media_ok = coverage["audio_bound"] == coverage["audio_required"] and coverage["images_ready"] == coverage["images_required"]
            for i in items:
                if i["section"] == "listening" and not _cloud_asset(i.get("audio") or {}):
                    media_ok = False
                visuals = {(a["visual_role"], a["option_number"]): a for a in i.get("visuals", [])}
                if any(not _cloud_asset(visuals.get(k, {})) for k in visual_requirements(i["section"], i["type_slot"], i["content_json"])):
                    media_ok = False
            if not media_ok:
                reasons.append("필수 음원·이미지 S3 등록 정보 부족")
            batches = {r.candidate.batch_id for r in records if r.candidate.batch_id}
            for bid in batches:
                b = next((b for b in snapshot["batches"] if b["id"] == bid), None)
                # Old stopped attempts do not override the verified bank registration.
                # The execution lock still excludes a live generation/media worker.
                if not b or b["status"] != "succeeded" or b.get("pipeline_status") not in (
                    None, "", "succeeded", "paused", "failed", "interrupted"
                ):
                    reasons.append("생성·미디어 작업 완료 확인 필요")
                if any(r.candidate.batch_id == bid and r.candidate.source_key not in {i["source_key"] for i in items} for r in catalog.records):
                    reasons.append("다른 문항과 연결된 생성 묶음")
            result.append({"set_id": sid, "section": s["section"], "model": s["generator_version"],
                           "sequence": s["set_sequence"], "count": len(records), "coverage": coverage,
                           "complete": not records and len(archived) == expected_count,
                           "reasons": list(dict.fromkeys(reasons)), "records": records,
                           "token": _token({"set": s, "items": items, "local": [asdict(r) for r in records],
                                            "archived": sorted(archived), "target": self.reader.target,
                                            "root": str(self.root)})})
        return result

    def prepare(self, set_ids):
        chosen = {r["set_id"]: r for r in self.candidates()}
        if not set_ids:
            raise PublicationBlocked("정리할 세트를 선택하세요.")
        selected = []
        for sid in dict.fromkeys(set_ids):
            row = chosen.get(sid)
            if not row or row["reasons"] or not row["count"]:
                raise PublicationBlocked("정리 조건이 바뀌었거나 정리가 끝났습니다. 새로고침 후 다시 선택하세요.")
            selected.append(row)
        return {"target": self.reader.target, "root": str(self.root),
                "sets": [{k: r[k] for k in ("set_id", "token", "count", "section", "model", "sequence")} for r in selected]}

    def _check(self, plan):
        if plan.get("target") != self.reader.target or plan.get("root") != str(self.root) or not plan.get("sets"):
            raise PublicationBlocked("확인한 DB 대상이 다릅니다. 새로고침하세요.")
        rows = {r["set_id"]: r for r in self.candidates()}
        selected = []
        for expected in plan["sets"]:
            r = rows.get(expected["set_id"])
            if not r or r["token"] != expected["token"] or r["reasons"] or not r["count"]:
                raise PublicationBlocked("확인 이후 문제·승인·미디어·정리 상태가 바뀌었습니다. 새로고침 후 다시 선택하세요.")
            selected.append(r)
        return selected

    def _backup(self):
        artifact = _create_backup(self.root, BackupOptions(include_sources=False))
        name = f"before_set_cleanup_{uuid.uuid4().hex}.zip"
        target = self.root / "backups" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as f:
            f.write(artifact.data)
            f.flush()
            os.fsync(f.fileno())
        inspect_backup(target.read_bytes())
        return target

    def execute(self, plan):
        changed, backup = [], None
        try:
            with execution_lock(self.root), self.reader.connect(readonly=True) as pg, ExitStack() as stack:
                for lock in ("topik_bank:production_deployment", "topik_bank:set_membership", "topik_bank:item_sync"):
                    pg.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
                selected = self._check(plan)
                grouped = {}
                for row in selected:
                    for record in row["records"]:
                        path = (self.root / record.candidate.source_db).resolve()
                        if not path.is_relative_to(self.root) or path.parent not in {
                            (self.root / "data/types").resolve(), (self.root / "data/listening/types").resolve()
                        } or path.suffix != ".db":
                            raise PublicationBlocked("문제 저장 경로가 올바르지 않습니다.")
                        grouped.setdefault(path, []).append((row["set_id"], record.candidate))
                connections = {}
                for path in sorted(grouped):
                    c = stack.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)))
                    c.execute("PRAGMA foreign_keys=ON")
                    c.execute("BEGIN IMMEDIATE")
                    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
                        raise PublicationBlocked("로컬 settings 구조를 확인하세요. 정리 화면에서는 DB 구조를 변경하지 않습니다.")
                    connections[path] = c
                self._check(plan)
                backup = self._backup()
                self._check(plan)
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for path, c in connections.items():
                    for sid in {sid for sid, _ in grouped[path]}:
                        records = [r for set_id, r in grouped[path] if set_id == sid]
                        key = PREFIX + sid
                        previous = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                        old = json.loads(previous[0]) if previous else {}
                        receipt = {"set_id": sid, "target": self.reader.target, "cleaned_at": now,
                                   "backup": str(backup.relative_to(self.root)),
                                   "source_keys": sorted(set(old.get("source_keys", [])) | {r.source_key for r in records}),
                                   "batch_ids": sorted(set(old.get("batch_ids", [])) | {r.batch_id for r in records if r.batch_id})}
                        c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                  (key, json.dumps(receipt, ensure_ascii=False)))
                    ids = [(r.generated_id,) for _, r in grouped[path]]
                    c.executemany("DELETE FROM reviews WHERE generated_question_id=?", ids)
                    c.executemany("DELETE FROM generated_questions WHERE id=?", ids)
                # Each file commits independently; durable receipts make partial completion visible.
                for path, c in connections.items():
                    c.commit()
                    changed.extend(r.source_key for _, r in grouped[path])
            return {"complete": True, "changed": changed, "backup": str(backup)}
        except PublicationBlocked:
            raise
        except Exception:
            if changed:
                return {"complete": False, "changed": changed, "backup": str(backup)}
            raise PublicationBlocked("정리하지 못했습니다. 백업 저장 공간·DB 연결·실행 중인 작업을 확인하고 새로고침하세요.") from None
