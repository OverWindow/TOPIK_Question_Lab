"""Atomic word exercise publication with source and edit conflict detection."""
from pathlib import Path
import uuid

from psycopg.types.json import Jsonb

from topik_question_lab.common.vocabulary_models import (
    VocabularyQuestion, conjugates, digest, source_snapshot, validate_bundle,
)
from topik_question_lab.step_06_publication.bank_transfer import connection, database_identity
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR


MIGRATION = "017_vocabulary_questions"
QUESTION_FIELDS = tuple(VocabularyQuestion.model_fields)


def apply_question_migration(c):
    c.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:schema_migrations'))")
    if c.execute("SELECT to_regclass('topik_bank.vocabulary_questions')").fetchone()[0] is None:
        c.execute((MIGRATION_DIR / f"{MIGRATION}.sql").read_text(encoding="utf-8"))
    c.execute("""INSERT INTO topik_bank.schema_migrations(version) VALUES (%s)
        ON CONFLICT(version) DO NOTHING""", (MIGRATION,))


def prepare_question_schema(root: Path, url: str):
    from topik_question_lab.maintenance.bank_migrations import _local_config, _backup_database
    config = _local_config(root, url)
    with connection(url, readonly=False) as c:
        actual = Path(c.execute("SHOW data_directory").fetchone()[0]).resolve()
        if actual != Path(config["data_directory"]).resolve():
            raise ValueError("관리 대상 로컬 Bank가 아닙니다.")
        c.execute("SELECT pg_advisory_xact_lock(hashtext('topik_bank:schema_migrations'))")
        if c.execute("SELECT to_regclass('topik_bank.vocabulary_questions')").fetchone()[0]:
            return None
        backup = _backup_database(root, url, config, label="017")
        apply_question_migration(c)
    return backup


def question_status(snapshot):
    rows, word = snapshot["existing"], snapshot["word"]
    if not rows:
        return "missing"
    try:
        expected = set(range(1, 7) if conjugates(word) else range(1, 4))
        positions = {row["position"] for row in rows}
        if (positions not in (expected, expected | {7})
                or any(row["source_hash"] != digest(word) for row in rows)
                or len({row["generation_id"] for row in rows}) != 1):
            return "stale"
        for row in rows:
            VocabularyQuestion.model_validate({key: row[key] for key in QUESTION_FIELDS})
        return "ready"
    except ValueError:
        return "stale"


class VocabularyQuestionBank:
    def __init__(self, url):
        self.url = url

    def identity(self):
        with connection(self.url) as c:
            return digest(database_identity(c))

    def available(self):
        with connection(self.url) as c:
            return c.execute("SELECT to_regclass('topik_bank.vocabulary_questions') IS NOT NULL").fetchone()[0]

    @staticmethod
    def _snapshot(c, vocabulary_id, *, lock=False):
        row = c.execute("SELECT to_jsonb(v) FROM topik_bank.vocabulary v WHERE vocabulary_id=%s"
                        + (" FOR UPDATE" if lock else ""), (vocabulary_id,)).fetchone()
        if row is None:
            raise ValueError("단어가 삭제되었습니다. 대상을 다시 선택하세요.")
        existing = [r[0] for r in c.execute("SELECT to_jsonb(q) FROM topik_bank.vocabulary_questions q "
                    "WHERE vocabulary_id=%s ORDER BY position" + (" FOR UPDATE" if lock else ""), (vocabulary_id,))]
        return {"word": source_snapshot(row[0]), "existing": existing, "existing_hash": digest(existing)}

    def snapshot(self, vocabulary_id):
        with connection(self.url) as c:
            return self._snapshot(c, vocabulary_id)

    def select(self, *, level=None, query="", mode="missing", limit=100, ids=None):
        if level not in (None, 3, 4, 5, 6) or mode not in {"missing", "stale", "regenerate"} or (limit is not None and limit < 1):
            raise ValueError("단어 생성 범위를 확인하세요.")
        clauses, params = [], []
        if level is not None:
            clauses.append("topik_level=%s")
            params.append(level)
        if ids is not None:
            clauses.append("vocabulary_id=ANY(%s)")
            params.append(list(ids))
        if query.strip():
            pattern = "%" + query.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            columns = [key for key in source_snapshot({}) if key not in {"vocabulary_id", "topik_level", "part_of_speech"}]
            clauses.append("(" + " OR ".join(f"coalesce(to_jsonb(v)->>'{key}','') ILIKE %s ESCAPE '!'" for key in columns) + ")")
            params.extend([pattern] * len(columns))
        found = []
        with connection(self.url) as c:
            sql = "SELECT vocabulary_id FROM topik_bank.vocabulary v" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY vocabulary_id"
            cursor = c.execute(sql, params)
            for (vocabulary_id,) in cursor:
                snapshot = self._snapshot(c, vocabulary_id)
                if mode != "regenerate" and question_status(snapshot) != mode:
                    continue
                found.append(snapshot)
                if limit is not None and len(found) >= limit:
                    break
        return found

    def save(self, task, config, database_key):
        bundle = validate_bundle(task["checkpoint"]["candidate"], task["snapshot"]["word"])
        review = task["checkpoint"].get("review", {})
        candidate_hash = digest(bundle.model_dump())
        if not review.get("passed") or task["checkpoint"].get("reviewed_hash") != candidate_hash:
            raise ValueError("현재 문항의 검수가 완료되지 않았습니다.")
        generation_id = str(uuid.UUID(task["generation_id"]))
        expected = [q.model_dump() for q in bundle.questions]

        def matches(rows):
            return ([{key: row[key] for key in QUESTION_FIELDS} for row in rows] == expected
                    and all(str(row["generation_id"]) == generation_id and row["source_hash"] == task["source_hash"]
                            and row["provider"] == config["provider"] and row["model"] == config["model"] for row in rows))

        with connection(self.url, readonly=False) as c:
            if digest(database_identity(c)) != database_key:
                raise ValueError("생성을 시작한 DB와 현재 DB가 다릅니다.")
            current = self._snapshot(c, task["vocabulary_id"], lock=True)
            if matches(current["existing"]):
                return "reconciled"
            if digest(current["word"]) != task["source_hash"]:
                raise ValueError("생성 이후 원본 단어가 변경되었습니다. 다시 생성하세요.")
            if current["existing_hash"] != task["snapshot"]["existing_hash"]:
                raise ValueError("기존 문항이 변경되었습니다. 새 작업으로 다시 확인하세요.")
            for question in expected:
                fields = {"vocabulary_id": task["vocabulary_id"], **question,
                          "provider": config["provider"], "model": config["model"],
                          "generation_id": generation_id, "source_hash": task["source_hash"]}
                values = [Jsonb(value) if key in {"choices", "accepted_answers"} else value for key, value in fields.items()]
                updates = ",".join(f"{key}=EXCLUDED.{key}" for key in fields if key not in {"vocabulary_id", "position"})
                c.execute(f"INSERT INTO topik_bank.vocabulary_questions({','.join(fields)}) VALUES ({','.join(['%s'] * len(fields))}) "
                          f"ON CONFLICT(vocabulary_id,position) DO UPDATE SET {updates},updated_at=CURRENT_TIMESTAMP", values)
            c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE vocabulary_id=%s AND NOT(position=ANY(%s))",
                      (task["vocabulary_id"], [q.position for q in bundle.questions]))
            if not matches(self._snapshot(c, task["vocabulary_id"])["existing"]):
                raise ValueError("PostgreSQL 저장 내용이 검수 완료 문항과 일치하지 않아 저장을 취소했습니다.")
        return "saved"
