from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import ProviderResult, QuestionExample, Review
from .validation import discard_resolved_validation_issues, normalize_question_blank_markers


SCHEMA = """
CREATE TABLE IF NOT EXISTS examples (
    source_key TEXT PRIMARY KEY,
    source_exam TEXT NOT NULL,
    question_number INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system_prompt TEXT NOT NULL,
    analysis_guide TEXT NOT NULL,
    generation_count INTEGER NOT NULL,
    difficulty TEXT NOT NULL,
    question_type TEXT NOT NULL DEFAULT 'grammar_blank',
    prompt_mode TEXT NOT NULL DEFAULT 'standard',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    operation TEXT NOT NULL,
    prompt_system TEXT NOT NULL,
    prompt_user TEXT NOT NULL,
    result_json TEXT NOT NULL,
    question_type TEXT NOT NULL DEFAULT 'grammar_blank',
    prompt_mode TEXT NOT NULL DEFAULT 'standard',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS generated_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    position INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    validation_json TEXT NOT NULL DEFAULT '[]',
    edited_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS reviews (
    generated_question_id INTEGER PRIMARY KEY,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(generated_question_id) REFERENCES generated_questions(id)
);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_column(connection, "prompt_versions", "question_type", "TEXT NOT NULL DEFAULT 'grammar_blank'")
            self._ensure_column(connection, "prompt_versions", "prompt_mode", "TEXT NOT NULL DEFAULT 'standard'")
            self._ensure_column(connection, "runs", "question_type", "TEXT NOT NULL DEFAULT 'grammar_blank'")
            self._ensure_column(connection, "runs", "prompt_mode", "TEXT NOT NULL DEFAULT 'standard'")

    def delete_files(self) -> list[Path]:
        """Delete this SQLite database and its transient sidecar files."""
        deleted: list[Path] = []
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            Path(f"{self.path}-journal"),
        ):
            if candidate.exists():
                candidate.unlink()
                deleted.append(candidate)
        return deleted

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def upsert_examples(self, examples: list[QuestionExample]) -> int:
        inserted = 0
        with self.connect() as connection:
            for example in examples:
                existing = connection.execute(
                    "SELECT data_json FROM examples WHERE source_key = ?", (example.source_key,)
                ).fetchone()
                if existing:
                    current = QuestionExample.model_validate_json(existing["data_json"])
                    example.answer = current.answer if current.answer is not None else example.answer
                    example.grammar_point = current.grammar_point or example.grammar_point
                    example.rationale = current.rationale or example.rationale
                    example.approved = current.approved
                    example.enrichment_model = current.enrichment_model
                    example.enrichment_confidence = current.enrichment_confidence
                    example.question_type = current.question_type or example.question_type
                    example.highlight_text = current.highlight_text or example.highlight_text
                    example.passage = current.passage or example.passage
                    example.question_prompt = current.question_prompt or example.question_prompt
                    example.auxiliary_text = current.auxiliary_text or example.auxiliary_text
                    example.set_key = current.set_key or example.set_key
                    example.parse_warning = current.parse_warning or example.parse_warning
                else:
                    inserted += 1
                connection.execute(
                    """INSERT INTO examples(source_key, source_exam, question_number, data_json)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(source_key) DO UPDATE SET
                         source_exam=excluded.source_exam,
                         question_number=excluded.question_number,
                         data_json=excluded.data_json,
                         updated_at=CURRENT_TIMESTAMP""",
                    (example.source_key, example.source_exam, example.question_number, example.model_dump_json()),
                )
        return inserted

    def list_examples(self, approved_only: bool = False) -> list[QuestionExample]:
        with self.connect() as connection:
            rows = connection.execute("SELECT data_json FROM examples ORDER BY source_exam, question_number").fetchall()
        examples = [QuestionExample.model_validate_json(row["data_json"]) for row in rows]
        return [example for example in examples if example.approved] if approved_only else examples

    def save_example(self, example: QuestionExample) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO examples(source_key, source_exam, question_number, data_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_key) DO UPDATE SET
                     source_exam=excluded.source_exam,
                     question_number=excluded.question_number,
                     data_json=excluded.data_json,
                     updated_at=CURRENT_TIMESTAMP""",
                (example.source_key, example.source_exam, example.question_number, example.model_dump_json()),
            )
            if example.set_key and example.passage:
                rows = connection.execute("SELECT source_key, data_json FROM examples").fetchall()
                for row in rows:
                    sibling = QuestionExample.model_validate_json(row["data_json"])
                    if sibling.source_key == example.source_key or sibling.set_key != example.set_key:
                        continue
                    sibling.passage = example.passage
                    sibling.stem = "\n".join(
                        value.strip()
                        for value in (sibling.auxiliary_text, sibling.passage, sibling.question_prompt)
                        if value.strip()
                    )
                    connection.execute(
                        "UPDATE examples SET data_json = ?, updated_at = CURRENT_TIMESTAMP WHERE source_key = ?",
                        (sibling.model_dump_json(), sibling.source_key),
                    )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO settings(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (key, value),
            )

    def save_prompt_version(
        self,
        system_prompt: str,
        analysis_guide: str,
        generation_count: int,
        difficulty: str,
        question_type: str = "grammar_blank",
        prompt_mode: str = "standard",
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO prompt_versions(
                       system_prompt, analysis_guide, generation_count, difficulty, question_type, prompt_mode
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (system_prompt, analysis_guide, generation_count, difficulty, question_type, prompt_mode),
            )
            return int(cursor.lastrowid)

    def list_prompt_versions(self) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM prompt_versions ORDER BY id DESC").fetchall()]

    def save_run(
        self,
        result: ProviderResult,
        system_prompt: str,
        user_prompt: str,
        question_type: str = "grammar_blank",
        prompt_mode: str = "standard",
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO runs(
                       provider, model, operation, prompt_system, prompt_user, result_json, question_type, prompt_mode
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.provider,
                    result.model,
                    result.operation,
                    system_prompt,
                    user_prompt,
                    result.model_dump_json(),
                    question_type,
                    prompt_mode,
                ),
            )
            return int(cursor.lastrowid)

    def add_generated_questions(self, run_id: int, provider: str, model: str, questions: list[dict], issues: list[list[dict]]) -> None:
        with self.connect() as connection:
            for position, (question, question_issues) in enumerate(zip(questions, issues), start=1):
                connection.execute(
                    """INSERT INTO generated_questions(run_id, provider, model, position, data_json, validation_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (run_id, provider, model, position, json.dumps(question, ensure_ascii=False), json.dumps(question_issues, ensure_ascii=False)),
                )

    def list_runs(self, operation: str | None = None) -> list[dict]:
        sql = "SELECT * FROM runs"
        params: tuple = ()
        if operation:
            sql += " WHERE operation = ?"
            params = (operation,)
        sql += " ORDER BY id DESC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def list_generated(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT g.*, r.data_json AS review_json
                   FROM generated_questions g
                   LEFT JOIN reviews r ON r.generated_question_id = g.id
                   ORDER BY g.id DESC"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["question"] = normalize_question_blank_markers(
                json.loads(item["edited_json"] or item["data_json"])
            )
            item["validation"] = discard_resolved_validation_issues(
                item["question"], json.loads(item["validation_json"])
            )
            item["reviewed"] = bool(item["review_json"])
            item["review"] = json.loads(item["review_json"]) if item["review_json"] else Review().model_dump()
            result.append(item)
        return result

    def save_generated_edit(self, question_id: int, data: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE generated_questions SET edited_json = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), question_id),
            )

    def sync_generated_set_passage(self, question_id: int, set_id: str, passage: str) -> list[int]:
        """Keep a generated shared passage aligned within the same model run."""
        if not set_id.strip() or not passage.strip():
            return []
        updated_ids: list[int] = []
        with self.connect() as connection:
            target = connection.execute(
                "SELECT run_id FROM generated_questions WHERE id = ?", (question_id,)
            ).fetchone()
            if target is None:
                return updated_ids
            rows = connection.execute(
                "SELECT id, data_json, edited_json FROM generated_questions WHERE run_id = ?",
                (target["run_id"],),
            ).fetchall()
            for row in rows:
                data = json.loads(row["edited_json"] or row["data_json"])
                if row["id"] == question_id or data.get("set_id") != set_id:
                    continue
                data["passage"] = passage
                data["stem"] = "\n".join(
                    value.strip()
                    for value in (
                        data.get("auxiliary_text", ""),
                        passage,
                        data.get("question_prompt", ""),
                    )
                    if value.strip()
                )
                connection.execute(
                    "UPDATE generated_questions SET edited_json = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False), row["id"]),
                )
                updated_ids.append(int(row["id"]))
        return updated_ids

    def save_validation(self, question_id: int, issues: list[dict]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE generated_questions SET validation_json = ? WHERE id = ?",
                (json.dumps(issues, ensure_ascii=False), question_id),
            )

    def save_review(self, question_id: int, review: Review) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO reviews(generated_question_id, data_json) VALUES (?, ?)
                   ON CONFLICT(generated_question_id) DO UPDATE SET data_json=excluded.data_json, updated_at=CURRENT_TIMESTAMP""",
                (question_id, review.model_dump_json()),
            )
