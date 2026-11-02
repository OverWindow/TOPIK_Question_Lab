"""Read-only view of authoring records, bank contents, and media coverage."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from topik_question_lab.common.question_lifecycle import has_trash_column
from topik_question_lab.common.local_cleanup import cleanup_receipts

from topik_question_lab.step_06_publication.direct_publication import batch_set_id, make_package, _target_fingerprint
from topik_question_lab.step_06_publication.postgres_storage import _psycopg, _dict_rows, _uses_unversioned_schema
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog, reconcile_catalog, _sha256


REVIEW_LABELS = {"pending_review": "검수 대기", "reviewed": "승인", "pilot": "시범 사용",
                 "active": "사용 중", "retired": "퇴역"}
PHASE_LABELS = {"pending": "대기", "running": "생성 중", "processing": "처리 중",
                "generating": "문항 생성", "partial": "일부 생성",
                "succeeded": "완료", "failed": "실패", "paused": "중단", "interrupted": "중단",
                "retry_wait": "재시도 대기", "tts": "음성 생성", "compose": "음원 합성",
                "image": "이미지 생성", "chart": "도표 생성", "upload": "S3 업로드", "verify": "파일 검증",
                "register": "DB 등록"}


def _json(value):
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("잘못된 작업 기록")
    return parsed


def _read_local(root):
    inventory, batches, media, jobs, errors = [], [], [], [], []
    for section, directory in (("reading", "data/types"), ("listening", "data/listening/types")):
        for path in sorted((root / directory).glob("*.db")):
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
                    c.execute("BEGIN")
                    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    counts = {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                              if table in tables else 0 for table in ("examples", "generated_questions")}
                    counts["trashed_questions"] = c.execute("SELECT COUNT(*) FROM generated_questions WHERE trashed_at IS NOT NULL").fetchone()[0] if has_trash_column(c) else 0
                    counts["generated_questions"] -= counts["trashed_questions"]
                inventory.append({"section": section, "path": path.relative_to(root).as_posix(), **counts})
            except sqlite3.Error:
                errors.append(f"{path.name}: SQLite 문항 수를 읽지 못했습니다.")
    for filename in ("bulk_generation.db", "publication_jobs.db"):
        path = root / "data" / filename
        if not path.exists():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
                c.row_factory = sqlite3.Row
                c.execute("BEGIN")
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if filename == "bulk_generation.db":
                    if "batches" in tables:
                        # Do not read saved provider configuration or raw error responses.
                        columns = {r[1] for r in c.execute("PRAGMA table_info(batches)")}
                        names = [n for n in ("id", "section", "model", "provider", "status", "created_at",
                                             "pipeline_status", "next_attempt_at") if n in columns]
                        batches = [dict(r) for r in c.execute(f"SELECT {','.join(names)} FROM batches ORDER BY created_at DESC")]
                    if "media_tasks" in tables:
                        media = [dict(r) for r in c.execute("""SELECT batch_id,kind,status,snapshot_json,
                            result_json,attempts,next_attempt_at FROM media_tasks WHERE active ORDER BY rowid""")]
                elif "jobs" in tables:
                    jobs = [dict(r) for r in c.execute("SELECT id,status,created_at,updated_at FROM jobs ORDER BY created_at DESC")]
        except sqlite3.Error:
            errors.append(f"{filename}: 작업 기록을 읽지 못했습니다.")
    return inventory, batches, media, jobs, errors


def read_bank(database_url):
    result = {"available": False, "media_available": False, "items": [], "sets": [], "message": "PostgreSQL 미설정"}
    if not database_url:
        return result
    try:
        psycopg, _ = _psycopg()
        with psycopg.connect(database_url, connect_timeout=8, prepare_threshold=None,
                             options="-c default_transaction_read_only=on -c statement_timeout=15000") as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            if not _uses_unversioned_schema(c):
                result["message"] = "PostgreSQL 문항 스키마를 확인하세요. 조회 화면에서는 스키마를 변경하지 않습니다."
                return result

            def query(sql):
                cursor = c.execute(sql)
                return _dict_rows(cursor, cursor.fetchall())

            result["items"] = query("""SELECT item_id,source_key,section,type_slot,item_type,generator_model,
                generator_version,review_status,content_json,content_hash,set_id,position,created_at,
                stem,choices,correct_answer,explanation,primary_skill,target_level,source_provenance
                FROM topik_bank.items ORDER BY created_at DESC,source_key""")
            result["sets"] = query("""SELECT set_id,section,generator_version,set_sequence,review_status,published_at,default_target_level
                FROM topik_bank.question_sets ORDER BY published_at DESC,set_id""")
            columns = {(r[0], r[1]) for r in c.execute("""SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema='topik_bank'""")}
            required = {(t, col) for t in ("tts_audio_assets", "item_visual_assets")
                        for col in ("storage_url", "storage_backend", "file_sha256")}
            required |= {("item_audio_bindings", "is_current"), ("question_set_item_audio_bindings", "is_current")}
            if required <= columns:
                audio = query("""SELECT i.item_id,a.audio_asset_id,a.storage_url,a.narration_version,a.storage_backend,a.file_sha256
                    FROM topik_bank.items i
                    LEFT JOIN topik_bank.question_set_item_audio_bindings sb
                      ON sb.set_id=i.set_id AND sb.position=i.position AND sb.is_current
                    LEFT JOIN topik_bank.item_audio_bindings ib
                      ON ib.item_id=i.item_id AND ib.content_hash=i.content_hash AND ib.is_current
                    JOIN topik_bank.tts_audio_assets a ON a.audio_asset_id=COALESCE(sb.audio_asset_id,ib.audio_asset_id)
                    WHERE a.deleted_at IS NULL""")
                visuals = query("""SELECT i.item_id,a.visual_asset_id,a.visual_role,a.option_number,a.storage_url,a.storage_backend,a.file_sha256
                    FROM topik_bank.items i JOIN topik_bank.item_visual_assets a
                      ON a.item_id=i.item_id AND a.content_hash=i.content_hash AND a.is_current""")
                by_audio = {str(r["item_id"]): r for r in audio}
                by_visual = defaultdict(list)
                for row in visuals:
                    by_visual[str(row["item_id"])].append(row)
                for item in result["items"]:
                    item["audio"] = by_audio.get(str(item["item_id"]))
                    item["visuals"] = by_visual[str(item["item_id"])]
                result["media_available"] = True
        result.update(available=True, message="PostgreSQL 조회 완료" if result["media_available"] else "문항 조회 완료 · 미디어 스키마 확인 필요")
    except Exception:
        # Driver exceptions can include connection strings or passwords.
        result.update(available=False, media_available=False, items=[], sets=[],
                      message="PostgreSQL 조회 실패 · 연결 설정·권한·스키마를 확인하고 새로고침하세요.")
    return result


def visual_requirements(section, slot, question):
    required = {("choice", int(o["number"])) for o in question.get("visual_options", []) or []}
    if section == "reading" and slot == 10:
        required.add(("material", 1))
    return required


def bank_coverage(items):
    audio_ids, audio_count, visual_ids, visual_count, expected_visual = set(), 0, set(), 0, 0
    for item in items:
        audio = item.get("audio")
        if item["section"] == "listening" and audio and audio["storage_url"] and audio["narration_version"] == "exam_track_v4":
            audio_count += 1
            audio_ids.add(str(audio["audio_asset_id"]))
        required = visual_requirements(item["section"], item["type_slot"], item["content_json"])
        expected_visual += len(required)
        present = {(a["visual_role"], a["option_number"]): a for a in item.get("visuals", []) if a["storage_url"]}
        visual_count += len(required & present.keys())
        visual_ids.update(str(present[key]["visual_asset_id"]) for key in required & present.keys())
    return {"audio_files": len(audio_ids), "audio_bound": audio_count,
            "audio_required": sum(i["section"] == "listening" for i in items),
            "images_ready": visual_count, "images_required": expected_visual, "image_files": len(visual_ids)}


def preparation_progress(batch, records, tasks):
    """Count current-plan assets with successful upload/verification checkpoints."""
    result = {"audio_files": None, "audio_bound": None, "images_ready": None, "images_required": None}
    try:
        from topik_question_lab.step_03_generation.media_generation import media_plan
        plan = media_plan(make_package(batch, records, require_approval=False), None)
        parsed = [(t, _json(t["snapshot_json"]), _json(t["result_json"])) for t in tasks]
        verified = {r.get("file_sha256") for t, _, r in parsed if t["kind"] == "verify" and t["status"] == "succeeded"}
        audio_positions, audio_files, images_ready = set(), set(), 0
        for expected in plan:
            for task, snapshot, artifact in parsed:
                if task["status"] != "succeeded" or not artifact.get("file_sha256") or artifact["file_sha256"] not in verified:
                    continue
                if expected["kind"] == "audio" and task["kind"] == "compose" and snapshot.get("script") == expected["script"]:
                    audio_positions.update(expected["positions"])
                    audio_files.add(tuple(expected["positions"]))
                    break
                if expected["kind"] == "image" and task["kind"] in ("image", "chart") and all(
                    snapshot.get(k) == expected[k] for k in ("prompt", "chart_spec")
                ):
                    images_ready += 1
                    break
        return {"audio_files": len(audio_files), "audio_bound": len(audio_positions), "images_ready": images_ready,
                "images_required": sum(t["kind"] == "image" for t in plan)}
    except (ValueError, TypeError, KeyError):
        # An incomplete/invalid batch has no reliable complete media plan yet.
        return result


def load_overview(root: Path, database_url: str):
    root = Path(root)
    inventory, batches, media, jobs, errors = _read_local(root)
    catalog = collect_candidate_catalog(root)
    errors.extend(catalog.read_errors)
    bank = read_bank(database_url)
    cleaned = set()
    try:
        target = _target_fingerprint(database_url)
        cleaned = {key for r in cleanup_receipts(root) if r["target"] == target for key in r["source_keys"]}
    except Exception:
        errors.append("로컬 정리 이력을 읽지 못했습니다.")
    remote = {str(i["source_key"]): i for i in bank["items"]}
    local = {r.candidate.source_key: r for r in catalog.records}
    rows = reconcile_catalog(catalog, bank["items"], {k for k, v in remote.items() if v["set_id"]})
    for row in rows:
        record, stored = local.get(row["source_key"]), remote.get(row["source_key"])
        if not bank["available"]:
            row["status"] = "PG 확인 불가"
        elif record is None:
            row["status"] = "로컬 정리 완료" if row["source_key"] in cleaned else ("로컬 원본 없음" if not catalog.read_errors else "로컬 조회 불완전")
        elif stored is None:
            row["status"] = "미등록"
        else:
            row["status"] = "내용 일치" if _sha256(record.candidate.question) == _sha256(stored["content_json"]) else "내용 불일치"
        row.update(local_present=record is not None, postgres_present=stored is not None,
                   created_at=str(record.candidate.created_at if record else stored["created_at"]),
                   local_review=("승인" if record.candidate.review.get("approved") else "검수 대기") if record else "—",
                   postgres_review=REVIEW_LABELS.get(stored["review_status"], stored["review_status"]) if stored else "—",
                   batch_id=record.candidate.batch_id if record else None,
                   audio=stored.get("audio") if stored else None, visuals=stored.get("visuals", []) if stored else [],
                   set_id=str(stored["set_id"]) if stored and stored["set_id"] else "",
                   coverage=bank_coverage([stored]) if stored and bank["media_available"] else None)
    records_by_batch, tasks_by_batch, items_by_set = defaultdict(list), defaultdict(list), defaultdict(list)
    for record in catalog.records:
        records_by_batch[record.candidate.batch_id].append(record)
    for task in media:
        tasks_by_batch[task["batch_id"]].append(task)
    for item in bank["items"]:
        items_by_set[str(item["set_id"])].append(item)
    sets = {str(s["set_id"]): s for s in bank["sets"]}
    for batch in batches:
        records, tasks = records_by_batch[batch["id"]], tasks_by_batch[batch["id"]]
        set_id = batch_set_id(batch["section"], batch["id"])
        stored = items_by_set[set_id]
        batch.update(local_count=len(records), registered_count=len(stored) if bank["available"] else None,
                     set_id=set_id, review_status=REVIEW_LABELS.get(sets.get(set_id, {}).get("review_status"), "미등록"),
                     preparation=preparation_progress(batch, records, tasks),
                     coverage=bank_coverage(stored) if bank["media_available"] and stored else None,
                     task_statuses=dict(Counter(t["status"] for t in tasks)),
                     tasks=[{k: t[k] for k in ("kind", "status", "attempts", "next_attempt_at")} for t in tasks])
    for row in bank["sets"]:
        items = items_by_set[str(row["set_id"])]
        row["item_count"] = len(items)
        row["coverage"] = bank_coverage(items) if bank["media_available"] else None
    return {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "inventory": inventory,
            "rows": rows, "batches": batches, "jobs": jobs, "bank": bank, "errors": errors}
