"""Transactional publication and editing of PBT writing questions in the Hub."""
import uuid

from psycopg.types.json import Jsonb

from topik_question_lab.common.paths import MIGRATION_DIR
from topik_question_lab.common.question_bank.connection import connection
from topik_question_lab.common.writing_models import WritingQuestion


MIGRATION = "018_writing_sets"


def apply_writing_migration(c):
    c.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:schema_migrations'))")
    if c.execute("SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s", (MIGRATION,)).fetchone():
        return
    c.execute((MIGRATION_DIR / f"{MIGRATION}.sql").read_text(encoding="utf-8"))
    c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)", (MIGRATION,))


def validate_writing_set(records):
    if len(records) != 4:
        raise ValueError("51~54번이 각각 한 문항씩 있어야 세트로 저장할 수 있습니다.")
    questions = [(row, WritingQuestion.model_validate(row["question"])) for row in records]
    questions.sort(key=lambda pair: pair[1].type_slot)
    if [q.type_slot for _, q in questions] != [51, 52, 53, 54]:
        raise ValueError("쓰기 세트에는 51~54번이 중복 없이 각각 한 문항씩 필요합니다.")
    if (len({row["id"] for row, _ in questions}) != 4
            or any(row["id"] <= 0 or row["run_id"] <= 0 for row, _ in questions)):
        raise ValueError("원본 생성 문항 식별자를 확인하세요.")
    if any(not row[field].strip() for row, _ in questions for field in ("provider", "model")):
        raise ValueError("생성 서비스와 모델이 필요합니다.")
    return questions


class WritingQuestionBank:
    def __init__(self, url: str):
        self.url = url

    def available(self):
        with connection(self.url) as c:
            return c.execute("""SELECT to_regclass('topik_bank.writing_sets') IS NOT NULL
                AND to_regclass('topik_bank.writing_questions') IS NOT NULL""").fetchone()[0]

    def list_sets(self):
        with connection(self.url) as c:
            return [{**row[0], "items": row[1]} for row in c.execute("""
                SELECT to_jsonb(s),count(q.type_slot) FROM topik_bank.writing_sets s
                LEFT JOIN topik_bank.writing_questions q USING(set_id)
                GROUP BY s.set_id ORDER BY s.created_at DESC,s.set_id""")]

    def get_set(self, source_key):
        with connection(self.url) as c:
            row = c.execute("SELECT to_jsonb(s) FROM topik_bank.writing_sets s WHERE source_key=%s",
                            (source_key,)).fetchone()
            if row is None:
                return None
            result = row[0]
            result["questions"] = [r[0] for r in c.execute(
                "SELECT to_jsonb(q) FROM topik_bank.writing_questions q WHERE set_id=%s ORDER BY type_slot",
                (result["set_id"],),
            )]
        return result

    def save_question(self, original, draft):
        question = WritingQuestion.model_validate(draft)
        if question.type_slot != original["type_slot"]:
            raise ValueError("문항 번호는 변경할 수 없습니다.")
        set_id = uuid.UUID(str(original["set_id"]))
        with connection(self.url, readonly=False) as c:
            # Match set publication's lock order and compare the displayed question.
            parent = c.execute("""SELECT exam_format FROM topik_bank.writing_sets
                WHERE set_id=%s FOR UPDATE""", (set_id,)).fetchone()
            if parent is None:
                raise ValueError("세트가 없습니다. 목록을 새로고침하세요.")
            if parent[0] != question.exam_format:
                raise ValueError("세트와 문항의 시험 형식이 다릅니다. 현재 PBT만 수정할 수 있습니다.")
            current = c.execute("""SELECT source_generated_id,content_json
                FROM topik_bank.writing_questions WHERE set_id=%s AND type_slot=%s FOR UPDATE""",
                (set_id, question.type_slot)).fetchone()
            if current != (original["source_generated_id"], original["content_json"]):
                raise ValueError("문항이 변경되었거나 삭제되었습니다. 목록을 새로고침한 뒤 다시 수정하세요.")
            c.execute("""UPDATE topik_bank.writing_questions SET content_json=%s
                WHERE set_id=%s AND type_slot=%s""", (Jsonb(question.model_dump()), set_id, question.type_slot))
            return c.execute("""UPDATE topik_bank.writing_sets SET updated_at=CURRENT_TIMESTAMP
                WHERE set_id=%s RETURNING updated_at""", (set_id,)).fetchone()[0]

    def save_set(self, source_key, records):
        questions = validate_writing_set(records)
        if not source_key or not source_key.strip():
            raise ValueError("원본 문항 조합 식별자가 필요합니다.")
        metadata = {field: " / ".join(dict.fromkeys(row[field] for row, _ in questions))
                    for field in ("provider", "model")}
        with connection(self.url, readonly=False) as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"writing-set:{source_key}",))
            existing = c.execute("""SELECT set_id,exam_format,provider,model FROM topik_bank.writing_sets
                WHERE source_key=%s FOR UPDATE""", (source_key,)).fetchone()
            if existing and existing[1:] != ("pbt", metadata["provider"], metadata["model"]):
                raise ValueError("저장된 세트와 원본의 시험 형식·서비스·모델이 다릅니다.")
            set_id = existing[0] if existing else uuid.uuid4()
            if existing:
                members = c.execute("""SELECT type_slot,source_generated_id FROM topik_bank.writing_questions
                    WHERE set_id=%s ORDER BY type_slot FOR UPDATE""", (set_id,)).fetchall()
                if members != [(q.type_slot, row["id"]) for row, q in questions]:
                    raise ValueError("저장된 세트의 문항 구성이 달라 저장을 취소했습니다.")
                c.execute("UPDATE topik_bank.writing_sets SET updated_at=CURRENT_TIMESTAMP WHERE set_id=%s", (set_id,))
            else:
                c.execute("""INSERT INTO topik_bank.writing_sets(set_id,source_key,exam_format,provider,model)
                    VALUES (%s,%s,'pbt',%s,%s)""", (set_id, source_key, metadata["provider"], metadata["model"]))
            for row, question in questions:
                c.execute("""INSERT INTO topik_bank.writing_questions(set_id,type_slot,source_generated_id,content_json)
                    VALUES (%s,%s,%s,%s) ON CONFLICT(set_id,type_slot)
                    DO UPDATE SET content_json=EXCLUDED.content_json""",
                    (set_id, question.type_slot, row["id"], Jsonb(question.model_dump())),
                )
        return {"set_id": str(set_id), "created": existing is None}
