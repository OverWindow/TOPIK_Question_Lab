"""Use the existing local schema while keeping writing away from MCQ readers."""
import json
import uuid

from topik_question_lab.common.storage import Storage
from topik_question_lab.common.writing_models import WritingQuestion


class WritingStorage(Storage):
    def list_generated(self, run_id: int | None = None) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM generated_questions"
                + (" WHERE run_id=?" if run_id is not None else "") + " ORDER BY id DESC",
                (run_id,) if run_id is not None else (),
            ).fetchall()
        return [{**dict(row), "question": json.loads(row["edited_json"] or row["data_json"])} for row in rows]

    def publication_source_key(self, run_id: int, *, create: bool = False) -> str | None:
        # Persist identity before sending so retries also survive a lost DB response.
        key = f"writing-set-source:{run_id}"
        with self.connect() as connection:
            if create:
                if not connection.execute("SELECT 1 FROM generated_questions WHERE run_id=?", (run_id,)).fetchone():
                    raise ValueError("저장할 생성 실행을 찾을 수 없습니다.")
                connection.execute(
                    "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                    (key, f"writing:pbt:{uuid.uuid4()}"),
                )
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def selection_source_key(self, question_ids: list[int], *, create: bool = False) -> str | None:
        from topik_question_lab.common.question_bank.writing_postgres import validate_writing_set

        selected = set(question_ids)
        records = [row for row in self.list_generated() if row["id"] in selected]
        if len(question_ids) != 4 or len(selected) != 4:
            raise ValueError("51~54번을 각각 한 문항씩 선택하세요.")
        validate_writing_set(records)
        run_ids = {row["run_id"] for row in records}
        # Keep the identity of sets published before individual generation was added.
        if len(run_ids) == 1:
            run_id = next(iter(run_ids))
            if {row["id"] for row in self.list_generated(run_id)} == selected:
                return self.publication_source_key(run_id, create=create)
        key = "writing-set-selection:" + ",".join(map(str, sorted(selected)))
        with self.connect() as connection:
            if create:
                connection.execute(
                    "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                    (key, f"writing:pbt:{uuid.uuid4()}"),
                )
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def save_generated_edit(self, question_id: int, data: dict) -> None:
        question = WritingQuestion.model_validate(data)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM generated_questions WHERE id=?", (question_id,)
            ).fetchone()
            if row is None:
                raise ValueError("저장할 문항을 찾을 수 없습니다.")
            if question.type_slot != json.loads(row["data_json"])["type_slot"]:
                raise ValueError("저장된 문항의 유형은 변경할 수 없습니다.")
            connection.execute(
                "UPDATE generated_questions SET edited_json=?, validation_json='[]' WHERE id=?",
                (question.model_dump_json(), question_id),
            )
