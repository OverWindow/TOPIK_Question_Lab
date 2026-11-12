"""Durable word tasks in the existing backed-up generation database."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from topik_question_lab.common.vocabulary.vocabulary_models import digest


SCHEMA = """
CREATE TABLE IF NOT EXISTS vocabulary_batches (
    batch_id TEXT NOT NULL PRIMARY KEY,
    database_key TEXT NOT NULL,
    config_json TEXT NOT NULL CHECK(json_valid(config_json) AND json_type(config_json)='object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','paused','ready','partial','failed','completed')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS vocabulary_tasks (
    generation_id TEXT NOT NULL PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES vocabulary_batches(batch_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    vocabulary_id INTEGER NOT NULL CHECK(vocabulary_id>0),
    source_hash TEXT NOT NULL CHECK(length(source_hash)=64 AND source_hash NOT GLOB '*[^a-f0-9]*'),
    snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json) AND json_type(snapshot_json)='object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','retry_wait','ready','needs_attention','failed','skipped','saved')),
    stage TEXT NOT NULL DEFAULT 'generate' CHECK(stage IN ('generate','review','repair','save')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    repair_count INTEGER NOT NULL DEFAULT 0 CHECK(repair_count BETWEEN 0 AND 1),
    next_attempt_at REAL NOT NULL DEFAULT 0 CHECK(next_attempt_at>=0),
    checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json) AND json_type(checkpoint_json)='object'),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(batch_id,vocabulary_id)
);
CREATE INDEX IF NOT EXISTS vocabulary_tasks_queue ON vocabulary_tasks(batch_id,status,next_attempt_at);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def decoded(row):
    if row is None:
        raise ValueError("단어 생성 작업을 찾을 수 없습니다.")
    value = dict(row)
    for key in ("config_json", "snapshot_json", "checkpoint_json"):
        if key in value:
            value[key.removesuffix("_json")] = json.loads(value.pop(key))
    return value


class VocabularyStore:
    def __init__(self, root: Path):
        self.path = Path(root) / "data/bulk_generation.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def create(self, database_key, config, snapshots):
        if not snapshots:
            raise ValueError("생성할 단어가 없습니다.")
        batch_id = str(uuid.uuid4())
        with self.connect() as c:
            c.execute("INSERT INTO vocabulary_batches(batch_id,database_key,config_json) VALUES (?,?,?)",
                      (batch_id, database_key, encode(config)))
            for snapshot in snapshots:
                c.execute("""INSERT INTO vocabulary_tasks(generation_id,batch_id,vocabulary_id,source_hash,snapshot_json)
                    VALUES (?,?,?,?,?)""", (str(uuid.uuid4()), batch_id, snapshot["word"]["vocabulary_id"],
                                           digest(snapshot["word"]), encode(snapshot)))
        return batch_id

    def batches(self, database_key):
        with self.connect() as c:
            return [decoded(row) for row in c.execute(
                "SELECT * FROM vocabulary_batches WHERE database_key=? ORDER BY created_at DESC,rowid DESC", (database_key,))]

    def batch(self, batch_id):
        with self.connect() as c:
            return decoded(c.execute("SELECT * FROM vocabulary_batches WHERE batch_id=?", (batch_id,)).fetchone())

    def tasks(self, batch_id):
        with self.connect() as c:
            return [decoded(row) for row in c.execute(
                "SELECT * FROM vocabulary_tasks WHERE batch_id=? ORDER BY vocabulary_id", (batch_id,))]

    def task(self, generation_id):
        with self.connect() as c:
            return decoded(c.execute("SELECT * FROM vocabulary_tasks WHERE generation_id=?", (generation_id,)).fetchone())

    def update(self, generation_id, **fields):
        allowed = {"status", "stage", "attempts", "repair_count", "next_attempt_at", "checkpoint", "error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("잘못된 작업 필드입니다.")
        values = {key + "_json" if key == "checkpoint" else key: encode(value) if key == "checkpoint" else value
                  for key, value in fields.items()}
        with self.connect() as c:
            c.execute(f"UPDATE vocabulary_tasks SET {','.join(key+'=?' for key in values)},updated_at=CURRENT_TIMESTAMP WHERE generation_id=?",
                      (*values.values(), generation_id))

    def set_batch(self, batch_id, status, error=""):
        with self.connect() as c:
            c.execute("UPDATE vocabulary_batches SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE batch_id=?",
                      (status, error, batch_id))

    def summarize(self, batch_id):
        states = [task["status"] for task in self.tasks(batch_id)]
        if all(state in {"saved", "skipped"} for state in states):
            status = "completed"
        elif all(state in {"ready", "saved", "skipped"} for state in states):
            status = "ready"
        elif any(state in {"ready", "saved"} for state in states):
            status = "partial"
        else:
            status = "failed"
        self.set_batch(batch_id, status)

    def recover(self):
        # Only call while owning the project execution lock.
        with self.connect() as c:
            c.executescript(SCHEMA)
            c.execute("UPDATE vocabulary_batches SET status='paused',updated_at=CURRENT_TIMESTAMP WHERE status='running'")
            ids = [r[0] for r in c.execute("""SELECT generation_id FROM vocabulary_tasks
                WHERE status NOT IN ('saved','skipped')
                AND coalesce(json_extract(checkpoint_json,'$.restart_required'),0)=0""")]
        for task_id in ids:
            self.reset_for_next_run(task_id)

    def reset_for_next_run(self, task_id, *, error=None):
        task = self.task(task_id)
        if task["status"] in {"saved", "skipped"}:
            return
        checkpoint = task["checkpoint"]
        if not checkpoint.get("restart_required"):
            checkpoint["last_attempt"] = {key: task[key] for key in ("stage", "status", "attempts", "repair_count")}
            checkpoint["last_attempt"]["reviewed_hash"] = checkpoint.pop("reviewed_hash", None)
            checkpoint["restart_required"] = True
        self.update(task_id, stage="generate", status="pending", attempts=0, repair_count=0,
                    next_attempt_at=0, checkpoint=checkpoint, error=task["error"] if error is None else error)
