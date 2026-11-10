"""Search and manually edit existing PostgreSQL word exercises."""
from psycopg.types.json import Jsonb

from topik_question_lab.common.vocabulary_models import VocabularyQuestion, digest, source_snapshot
from topik_question_lab.step_06_publication.bank_transfer import connection, database_identity
from topik_question_lab.step_06_publication.vocabulary_questions import QUESTION_FIELDS, VocabularyQuestionBank


PAGE_SIZE = 50
TYPES = {"sentence_blank": "문장 빈칸", "conjugation": "활용형", "synonym": "유의어", "antonym": "반의어"}
EDIT_FIELDS = tuple(key for key in QUESTION_FIELDS if key not in {"question_type", "position"})


def search_questions(url, *, query="", level=None, question_type=None, page=1):
    if level not in (None, 3, 4, 5, 6) or question_type not in (None, *TYPES) or page < 1:
        raise ValueError("급수·문제 유형·페이지를 확인하세요.")
    result = {"available": False, "message": "단어 문제 DB 연결이 설정되지 않았습니다.",
              "rows": [], "total": 0, "page": 1, "pages": 1}
    if not url.strip():
        return result
    clauses, params = [], []
    if level is not None:
        clauses.append("v.topik_level=%s")
        params.append(level)
    if question_type is not None:
        clauses.append("q.question_type=%s")
        params.append(question_type)
    if query.strip():
        pattern = "%" + query.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        # JSON access also supports banks without the optional language columns.
        columns = [f"coalesce(to_jsonb(v)->>'{key}', '')" for key in source_snapshot({})
                   if key not in {"vocabulary_id", "topik_level"}]
        columns += ["q.question_id::text", "q.instruction", "q.stem", "q.choices::text",
                    "q.accepted_answers::text", "q.explanation"]
        clauses.append("(" + " OR ".join(f"{column} ILIKE %s ESCAPE '!'" for column in columns) + ")")
        params.extend([pattern] * len(columns))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    joined = " FROM topik_bank.vocabulary_questions q JOIN topik_bank.vocabulary v USING(vocabulary_id)"
    with connection(url) as c:
        if not all(c.execute("SELECT to_regclass('topik_bank.vocabulary'), "
                             "to_regclass('topik_bank.vocabulary_questions')").fetchone()):
            result["message"] = "단어 문제 테이블이 아직 준비되지 않았습니다. 단어 연습문제 생성 메뉴에서 준비하세요."
            return result
        total = c.execute("SELECT count(*)" + joined + where, params).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        rows = [row[0] for row in c.execute(
            "SELECT to_jsonb(q) || jsonb_build_object('korean', v.korean, 'part_of_speech', v.part_of_speech, "
            "'topik_level', v.topik_level)" + joined + where
            + " ORDER BY v.topik_level, v.korean, v.vocabulary_id, q.position LIMIT %s OFFSET %s",
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE])]
    return {**result, "available": True, "message": "", "rows": rows, "total": total, "pages": pages, "page": page}


def load_question(url, question_id):
    with connection(url) as c:
        row = c.execute("SELECT vocabulary_id FROM topik_bank.vocabulary_questions WHERE question_id=%s",
                        (question_id,)).fetchone()
        if row is None:
            raise ValueError("문항이 삭제되었습니다. 새로고침 후 다시 선택하세요.")
        return {**VocabularyQuestionBank._snapshot(c, row[0]), "question_id": question_id,
                "database_key": digest(database_identity(c))}


def selected_question(snapshot):
    return next(row for row in snapshot["existing"] if row["question_id"] == snapshot["question_id"])


def _check_current(c, snapshot):
    if digest(database_identity(c)) != snapshot["database_key"]:
        raise ValueError("문항을 불러온 DB와 현재 DB가 다릅니다. 새로고침하세요.")
    # Lock the word first, just as generation/save does, to serialize both workflows.
    current = VocabularyQuestionBank._snapshot(c, snapshot["word"]["vocabulary_id"], lock=True)
    if digest(current["word"]) != digest(snapshot["word"]) or current["existing_hash"] != snapshot["existing_hash"]:
        raise ValueError("불러온 뒤 단어 또는 문항이 변경되었습니다. 새로고침 후 다시 확인하세요.")


def update_question(url, snapshot, payload):
    question = VocabularyQuestion.model_validate(payload).model_dump()
    original = selected_question(snapshot)
    if any(question[key] != original[key] for key in ("question_type", "position")):
        raise ValueError("문제 유형과 문항 순서는 변경할 수 없습니다.")
    with connection(url, readonly=False) as c:
        _check_current(c, snapshot)
        values = [Jsonb(question[key]) if key in {"choices", "accepted_answers"} else question[key]
                  for key in EDIT_FIELDS]
        row = c.execute("UPDATE topik_bank.vocabulary_questions SET "
                        + ",".join(f"{key}=%s" for key in EDIT_FIELDS)
                        + ",updated_at=CURRENT_TIMESTAMP WHERE question_id=%s RETURNING question_id",
                        [*values, snapshot["question_id"]]).fetchone()
        current = VocabularyQuestionBank._snapshot(c, snapshot["word"]["vocabulary_id"])
        expected = [{**q, **question} if q["question_id"] == snapshot["question_id"] else q
                    for q in snapshot["existing"]]
        # Ignore only the edit timestamp when checking stored content and provenance.
        def without_timestamp(rows):
            return [{k: v for k, v in q.items() if k != "updated_at"} for q in rows]

        if row is None or without_timestamp(current["existing"]) != without_timestamp(expected):
            raise ValueError("PostgreSQL 저장 내용이 수정본과 일치하지 않아 저장을 취소했습니다.")


def delete_question(url, snapshot):
    with connection(url, readonly=False) as c:
        _check_current(c, snapshot)
        row = c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE question_id=%s RETURNING question_id",
                        (snapshot["question_id"],)).fetchone()
        expected = [q for q in snapshot["existing"] if q["question_id"] != snapshot["question_id"]]
        current = VocabularyQuestionBank._snapshot(c, snapshot["word"]["vocabulary_id"])
        if row is None or current["existing"] != expected:
            raise ValueError("PostgreSQL 삭제 결과가 일치하지 않아 삭제를 취소했습니다.")
