"""Atomic generation writes shared by the reading and listening stores."""
from __future__ import annotations

import json
import sqlite3


def ensure_generation_schema(connection: sqlite3.Connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
    if "batch_id" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN batch_id TEXT")
    connection.execute("""CREATE TABLE IF NOT EXISTS generation_receipts (
        batch_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
        question_ids_json TEXT NOT NULL, review_count INTEGER NOT NULL DEFAULT 0
    )""")


class GenerationStorageMixin:
    def reference_examples(self, example_cls, approved_only=False):
        """Read PBT references without relabelling or copying them into IBT storage."""
        from topik_question_lab.common.generation.exam_profiles import profile_for
        section = "listening" if example_cls.__name__.startswith("Listening") else "reading"
        profile = profile_for(section, self.path.stem)
        source = self.path.with_name(profile.base_type_id + ".db")
        if not source.exists():
            return []
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT data_json FROM examples ORDER BY source_exam,question_number").fetchall()
        values = [example_cls.model_validate_json(row[0]) for row in rows]
        return [v for v in values if v.approved] if approved_only else values

    def approve_generation(self, batch_id: str) -> None:
        from topik_question_lab.common.models import Review
        from topik_question_lab.common.question_lifecycle import active_condition

        review = Review(approved=True, notes=("30문항" if self.path.stem.startswith("ibt_") else "50문항") + " 생성 완료로 자동 승인").model_dump_json()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT run_id,question_ids_json FROM generation_receipts WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if receipt is None:
                raise ValueError("생성 영수증이 없어 자동 승인할 수 없습니다.")
            ids = [row["id"] for row in connection.execute(
                f"SELECT g.id FROM generated_questions g WHERE run_id=? AND {active_condition(connection)} ORDER BY position",
                (receipt["run_id"],),
            )]
            if ids != json.loads(receipt["question_ids_json"]):
                raise ValueError("생성 문항이 삭제되거나 변경되어 자동 승인할 수 없습니다.")
            # Keep any approval or rejection already recorded by a person.
            connection.executemany(
                "INSERT INTO reviews(generated_question_id,data_json) VALUES (?,?) ON CONFLICT(generated_question_id) DO NOTHING",
                [(question_id, review) for question_id in ids],
            )

    def generation_receipt(self, batch_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM generation_receipts WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None:
                return None
            receipt = dict(row)
            ids = json.loads(receipt.pop("question_ids_json"))
            rows = connection.execute(
                "SELECT id, edited_json, data_json FROM generated_questions WHERE run_id=? ORDER BY position",
                (receipt["run_id"],),
            ).fetchall()
            if [row["id"] for row in rows] != ids:
                raise ValueError("저장된 생성 묶음의 문항 연결이 손상되었습니다.")
            receipt["question_ids"] = ids
            receipt["slots"] = [json.loads(row["edited_json"] or row["data_json"])["type_slot"] for row in rows]
            receipt["count"] = len(ids)
            return receipt

    def save_generation_result(
        self, result, system: str, user: str, question_type: str, prompt_mode: str,
        questions: list[dict] | None = None, issues: list[list[dict]] | None = None,
        batch_id: str | None = None,
    ) -> dict:
        if questions is not None and len(questions) != len(issues or []):
            raise ValueError("문항과 검사 결과 수가 일치하지 않습니다.")
        with self.connect() as connection:
            # The receipt and its questions commit together. BEGIN IMMEDIATE also
            # serializes a duplicate submission from another connection.
            connection.execute("BEGIN IMMEDIATE")
            if batch_id and questions is not None:
                row = connection.execute(
                    "SELECT * FROM generation_receipts WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if row:
                    return {"run_id": row["run_id"], "question_ids": json.loads(row["question_ids_json"]),
                            "review_count": row["review_count"]}
            cursor = connection.execute(
                """INSERT INTO runs(provider,model,operation,prompt_system,prompt_user,
                   result_json,question_type,prompt_mode,batch_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                (result.provider, result.model, result.operation, system, user,
                 result.model_dump_json(), question_type, prompt_mode, batch_id),
            )
            run_id = int(cursor.lastrowid)
            ids = []
            for position, (question, group) in enumerate(zip(questions or [], issues or []), 1):
                cursor = connection.execute(
                    """INSERT INTO generated_questions(run_id,provider,model,position,data_json,validation_json)
                       VALUES (?,?,?,?,?,?)""",
                    (run_id, result.provider, result.model, position,
                     json.dumps(question, ensure_ascii=False), json.dumps(group, ensure_ascii=False)),
                )
                ids.append(int(cursor.lastrowid))
            review_count = sum(bool(group) for group in issues or [])
            if batch_id and questions is not None:
                connection.execute(
                    "INSERT INTO generation_receipts VALUES (?,?,?,?)",
                    (batch_id, run_id, json.dumps(ids), review_count),
                )
            return {"run_id": run_id, "question_ids": ids, "review_count": review_count}
