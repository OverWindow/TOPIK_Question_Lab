"""Reversible disposal of unregistered, individually generated questions."""
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from topik_question_lab.common.question_lifecycle import has_trash_column
from topik_question_lab.step_06_publication.direct_publication import DirectPublicationService, PublicationBlocked, _hash
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog


def trash_token(record):
    return _hash({"candidate": asdict(record.candidate), "trashed_at": record.trashed_at})


class QuestionTrash:
    def __init__(self, root, url):
        self.root = Path(root).resolve()
        self.reader = DirectPublicationService(root, url, readonly=True)

    def _selected(self, expected, restore):
        catalog = collect_candidate_catalog(self.root, include_trashed=True)
        if catalog.read_errors:
            raise PublicationBlocked("문항 기록을 모두 읽지 못했습니다. 저장 현황을 먼저 확인하세요.")
        records = {r.candidate.source_key: r for r in catalog.records}
        selected = []
        for key, token in expected.items():
            r = records.get(key)
            if not r or trash_token(r) != token:
                raise PublicationBlocked("선택 이후 문제·검수·휴지통 상태가 바뀌었습니다. 새로고침 후 다시 선택하세요.")
            if (r.trashed_at is not None) != restore:
                raise PublicationBlocked("현재 휴지통 상태와 요청이 다릅니다. 새로고침하세요.")
            if r.candidate.batch_id:
                raise PublicationBlocked("일괄 생성 묶음의 문제는 개별적으로 버리거나 복원할 수 없습니다.")
            selected.append(r)
        return selected

    def apply(self, expected, *, restore=False):
        if not expected:
            raise PublicationBlocked("처리할 문제를 선택하세요.")
        changed = []
        try:
            with self.reader.connect(readonly=True) as pg, ExitStack() as stack:
                # Match registration's lock order so registration cannot race disposal.
                for lock in ("topik_bank:production_deployment", "topik_bank:set_membership", "topik_bank:item_sync"):
                    pg.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
                selected = self._selected(expected, restore)
                exists = pg.execute("""SELECT source_key FROM topik_bank.items
                    WHERE source_key=ANY(%s) OR item_id=ANY(%s::uuid[]) LIMIT 1""",
                    (list(expected), [str(r.candidate.item_id) for r in selected])).fetchone()
                if exists:
                    raise PublicationBlocked("이미 PostgreSQL에 등록된 문제가 포함되어 있습니다. 미등록 문제만 처리할 수 있습니다.")
                grouped = {}
                for r in selected:
                    path = (self.root / r.candidate.source_db).resolve()
                    allowed = {(self.root / "data/types").resolve(), (self.root / "data/listening/types").resolve()}
                    if path.parent not in allowed or not path.is_relative_to(self.root) or path.suffix != ".db":
                        raise PublicationBlocked("문제 저장 경로가 올바르지 않습니다.")
                    grouped.setdefault(path, []).append(r)
                connections = {}
                for path in sorted(grouped):
                    c = stack.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)))
                    c.execute("BEGIN IMMEDIATE")
                    connections[path] = c
                # Re-read after acquiring every local write lock, before any mutation.
                self._selected(expected, restore)
                for path, c in connections.items():
                    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "generation_receipts" in tables:
                        ids = {r.candidate.generated_id for r in grouped[path]}
                        runs = {r.candidate.run_id for r in grouped[path]}
                        for run_id, raw in c.execute("SELECT run_id,question_ids_json FROM generation_receipts"):
                            if run_id in runs or ids.intersection(json.loads(raw)):
                                raise PublicationBlocked("일괄 생성 기록에 연결된 문제는 개별적으로 처리할 수 없습니다.")
                timestamp = None if restore else datetime.now(timezone.utc).isoformat(timespec="seconds")
                for path, c in connections.items():
                    if not has_trash_column(c):
                        c.execute("ALTER TABLE generated_questions ADD COLUMN trashed_at TEXT")
                    c.executemany("UPDATE generated_questions SET trashed_at=? WHERE id=?",
                                  [(timestamp, r.candidate.generated_id) for r in grouped[path]])
                # Files have independent transactions; report confirmed progress if a disk commit fails.
                for path, c in connections.items():
                    c.commit()
                    changed.extend(r.candidate.source_key for r in grouped[path])
            return {"complete": True, "changed": changed, "restore": restore}
        except PublicationBlocked:
            raise
        except Exception:
            if changed:
                return {"complete": False, "changed": changed, "restore": restore,
                        "message": "일부 DB 저장을 완료하지 못했습니다. 새로고침 후 휴지통 상태를 확인하세요."}
            raise PublicationBlocked("휴지통 처리를 완료하지 못했습니다. DB 연결·파일 잠금 상태를 확인하고 새로고침하세요.") from None
