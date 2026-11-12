"""Transfer complete word exercises and their words in one destination transaction."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from psycopg import sql
from psycopg.types.json import Jsonb

from topik_question_lab.common.vocabulary.vocabulary_models import source_snapshot
from topik_question_lab.menus.vocabulary_transfer import vocabulary_transfer as vocabulary
from topik_question_lab.common.question_bank.connection import TransferError, _distinct_target, connection, database_identity, digest
from topik_question_lab.common.vocabulary.vocabulary_questions import QUESTION_FIELDS, question_status


SCHEMA_FILE = vocabulary.MIGRATION_DIR / "017_vocabulary_questions.sql"
COPY_FIELDS = ("vocabulary_id", *QUESTION_FIELDS, "provider", "model", "generation_id", "source_hash",
               "created_at", "updated_at")


def _schema(c):
    relation = c.execute("""SELECT relkind,relrowsecurity,relforcerowsecurity FROM pg_class
        WHERE oid=to_regclass('topik_bank.vocabulary_questions')""").fetchone()
    if relation is None:
        return None
    if relation != ("r", False, False):
        raise TransferError("단어 문제는 일반 테이블과 행 보안이 없는 구조가 필요합니다.")
    if c.execute("""SELECT EXISTS(SELECT 1 FROM pg_trigger
        WHERE tgrelid='topik_bank.vocabulary_questions'::regclass AND NOT tgisinternal AND tgenabled<>'D')""").fetchone()[0]:
        raise TransferError("단어 문제에 사용자 트리거가 있어 자동 등록할 수 없습니다.")
    columns = c.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,
        pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='topik_bank.vocabulary_questions'::regclass AND a.attnum>0 AND NOT a.attisdropped""").fetchall()
    types = {"question_id": "bigint", "vocabulary_id": "bigint", "position": "smallint",
             "correct_answer": "smallint", "choices": "jsonb", "accepted_answers": "jsonb",
             "generation_id": "uuid", "created_at": "timestamp with time zone", "updated_at": "timestamp with time zone"}
    defaults = {"accepted_answers": "'[]'::jsonb", "created_at": "CURRENT_TIMESTAMP", "updated_at": "CURRENT_TIMESTAMP"}
    expected = [(key, types.get(key, "text"), True, "a" if key == "question_id" else "", defaults.get(key))
                for key in ("question_id", *COPY_FIELDS)]
    if sorted(columns) != sorted(expected):
        raise TransferError("단어 문제 컬럼·형식·기본값·자동 증가 구조가 지원 형식과 다릅니다.")
    constraints = c.execute("""SELECT pg_get_constraintdef(oid),convalidated FROM pg_constraint
        WHERE conrelid='topik_bank.vocabulary_questions'::regclass AND contype<>'n'""").fetchall()
    indexes = c.execute("""SELECT pg_get_indexdef(indexrelid),indisvalid,indisready FROM pg_index
        WHERE indrelid='topik_bank.vocabulary_questions'::regclass""").fetchall()
    sequence = c.execute("""SELECT seqincrement,seqmin,seqmax,seqcache,seqcycle FROM pg_sequence
        WHERE seqrelid=pg_get_serial_sequence('topik_bank.vocabulary_questions','question_id')::regclass""").fetchone()
    if sequence != (1, 1, 9223372036854775807, 1, False) or any(not valid for _, valid in constraints):
        raise TransferError("단어 문제 제약 또는 자동 증가 번호 설정을 확인하세요.")
    return {"columns": sorted(list(row) for row in columns), "constraints": sorted(list(row) for row in constraints),
            "indexes": sorted([definition.split(" USING ", 1)[1], valid, ready] for definition, valid, ready in indexes),
            "sequence": list(sequence)}


def _rows(c):
    return [row[0] for row in c.execute(
        "SELECT to_jsonb(q) FROM topik_bank.vocabulary_questions q ORDER BY vocabulary_id,position")]


def _sequence_state(c):
    schema, name = c.execute("""SELECT n.nspname,s.relname FROM pg_class s
        JOIN pg_namespace n ON n.oid=s.relnamespace
        WHERE s.oid=pg_get_serial_sequence('topik_bank.vocabulary_questions','question_id')::regclass""").fetchone()
    value, called = c.execute(sql.SQL("SELECT last_value,is_called FROM {}.{}").format(
        sql.Identifier(schema), sql.Identifier(name))).fetchone()
    return {"schema": schema, "name": name, "last_value": value, "is_called": called}


@dataclass
class QuestionSnapshot:
    vocabulary: vocabulary.VocabularySnapshot
    schema: dict
    questions: list[dict]
    excluded: list[dict]

    @property
    def signature(self):
        return digest((self.vocabulary.signature, self.schema, self.questions, self.excluded))


@dataclass
class QuestionTransferPlan:
    snapshot: QuestionSnapshot
    vocabulary: vocabulary.VocabularyTransferPlan
    target_signature: str
    create_table: bool
    effects: dict
    target_count: int


def capture_questions(source_url):
    with connection(source_url) as c:
        word_schema, schema = vocabulary._schema(c), _schema(c)
        if word_schema is None or schema is None:
            raise TransferError("단어 연습문제 생성 메뉴에서 단어장과 문제 테이블을 먼저 준비하세요.")
        grouped = defaultdict(list)
        for question in _rows(c):
            grouped[question["vocabulary_id"]].append(question)
        words, questions, excluded = [], [], []
        for word in vocabulary._rows(c):
            bundle = grouped[word["vocabulary_id"]]
            state = question_status({"word": source_snapshot(word), "existing": bundle})
            if state == "ready":
                words.append(word)
                questions.extend(bundle)
            else:
                excluded.append({"ID": word["vocabulary_id"], "단어": word["korean"],
                                 "사유": "미생성" if state == "missing" else "문항 누락·원본 변경·형식 오류"})
        return QuestionSnapshot(vocabulary.VocabularySnapshot(database_identity(c), word_schema, words),
                                schema, questions, excluded)


def _key(row):
    return row["vocabulary_id"], row["position"]


def _content(row):
    return {key: row[key] for key in COPY_FIELDS}


def _effects(snapshot, previous_words, previous):
    by_key = {_key(row): row for row in previous}
    source_keys = {_key(row) for row in snapshot.questions}
    words = {row["vocabulary_id"]: row for row in snapshot.vocabulary.rows}
    effective = vocabulary._effective_rows(snapshot.vocabulary.rows, previous_words)
    conflicts = []
    for word in effective:
        if source_snapshot(word) != source_snapshot(words[word["vocabulary_id"]]):
            conflicts.append({"ID": word["vocabulary_id"], "단어": word["korean"],
                              "사유": "대상에 보존할 뜻이 문제의 원본 단어와 다름"})
    extras = sorted({row["vocabulary_id"] for row in previous
                     if row["vocabulary_id"] in words and _key(row) not in source_keys})
    conflicts.extend({"ID": key, "단어": words[key]["korean"], "사유": "같은 단어에 원본에 없는 추가 문항이 있음"}
                     for key in extras)
    result = {"created": 0, "updated": 0, "unchanged": 0, "conflicts": conflicts,
              "extra": sum(_key(row) not in source_keys for row in previous)}
    for row in snapshot.questions:
        old = by_key.get(_key(row))
        result["created" if old is None else "unchanged" if _content(old) == _content(row) else "updated"] += 1
    return result


def prepare_question_transfer(snapshot, target_url):
    if not snapshot.questions:
        raise TransferError("등록할 유효한 문제 묶음이 없습니다. 제외 사유를 확인하세요.")
    with connection(target_url) as c:
        word_plan = vocabulary._prepare_vocabulary_transfer(c, snapshot.vocabulary)
        schema = _schema(c)
        if schema is not None and schema != snapshot.schema:
            raise TransferError("대상 단어 문제의 제약·인덱스 구조가 원본과 다릅니다.")
        previous = _rows(c) if schema else []
        words = vocabulary._rows(c) if not word_plan.create_table else []
        return QuestionTransferPlan(snapshot, word_plan, digest((schema, previous)), schema is None,
                                    _effects(snapshot, words, previous), len(previous))


def _backup(root, identity, word_schema, words, word_sequence, schema, questions, sequence):
    state = {"vocabulary": {"schema": word_schema, "rows": words, "sequence": word_sequence},
             "vocabulary_questions": {"schema": schema, "rows": questions, "sequence": sequence}}
    payload = {"format": "topik-vocabulary-questions-backup-v1", "target_fingerprint": digest(identity),
               "created_at": datetime.now(timezone.utc).isoformat(), "tables": state, "content_hash": digest(state),
               "create_table_sql": {
                   "vocabulary": vocabulary.SCHEMA_FILE.read_text(encoding="utf-8")
                       + ("\n" + vocabulary.AUDIO_MIGRATION.read_text(encoding="utf-8") if vocabulary._has_audio(word_schema) else "")
                       + ("\n" + vocabulary.LANGUAGE_MIGRATION.read_text(encoding="utf-8") if vocabulary._has_languages(word_schema) else ""),
                   "vocabulary_questions": SCHEMA_FILE.read_text(encoding="utf-8")}}
    path = Path(root) / "backups" / "vocabulary" / f"before_questions_transfer_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise ValueError("backup mismatch")
    except (OSError, ValueError):
        raise TransferError("변경 전 단어·문제 백업에 실패했습니다. DB는 변경하지 않았습니다.") from None
    return path


def _write_words(c, snapshot, schema, previous, next_id):
    normalized = vocabulary._with_audio(previous) if vocabulary._has_audio(snapshot.schema) else previous
    if vocabulary._has_languages(snapshot.schema):
        normalized = vocabulary._with_languages(normalized)
    effective = vocabulary._effective_rows(snapshot.rows, normalized)
    expected = {row["vocabulary_id"]: row for row in normalized}
    changed = [row for row in effective if expected.get(row["vocabulary_id"]) != row]
    expected.update({row["vocabulary_id"]: row for row in effective})
    if schema is None:
        c.execute("CREATE SCHEMA IF NOT EXISTS topik_bank")
        c.execute(vocabulary.SCHEMA_FILE.read_text(encoding="utf-8"))
    if vocabulary._has_audio(snapshot.schema) and not vocabulary._has_audio(schema):
        vocabulary.apply_audio_migration(c)
    if vocabulary._has_languages(snapshot.schema) and not vocabulary._has_languages(schema):
        vocabulary.apply_language_migration(c)
    vocabulary._upsert(c, changed)
    sequence = vocabulary._sequence_state(c)
    if sequence["last_value"] + int(sequence["is_called"]) != next_id:
        c.execute(sql.SQL("ALTER TABLE topik_bank.vocabulary ALTER COLUMN vocabulary_id RESTART WITH {}")
                  .format(sql.Literal(next_id)))
    return sorted(expected.values(), key=lambda row: row["vocabulary_id"])


def _write_questions(c, rows, previous):
    expected = {_key(row): row for row in previous}
    changed = [row for row in rows if _key(row) not in expected or _content(expected[_key(row)]) != _content(row)]
    if changed:
        names = sql.SQL(",").join(map(sql.Identifier, COPY_FIELDS))
        updates = sql.SQL(",").join(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(key), sql.Identifier(key))
                                   for key in COPY_FIELDS if key not in {"vocabulary_id", "position"})
        saved = c.execute(sql.SQL("""INSERT INTO topik_bank.vocabulary_questions AS q ({names})
            SELECT {names} FROM jsonb_populate_recordset(NULL::topik_bank.vocabulary_questions,%s)
            ON CONFLICT(vocabulary_id,position) DO UPDATE SET {updates} RETURNING to_jsonb(q)""")
            .format(names=names, updates=updates), (Jsonb(changed),)).fetchall()
        returned = {_key(row[0]): row[0] for row in saved}
        for row in changed:
            actual = returned.get(_key(row))
            old = expected.get(_key(row))
            if (actual is None or _content(actual) != _content(row)
                    or old is not None and actual["question_id"] != old["question_id"]):
                raise TransferError("단어 문제 저장 내용 또는 기존 문제 ID가 예상 결과와 다릅니다.")
            expected[_key(row)] = actual
    return sorted(expected.values(), key=_key)


def _verify(c, snapshot, words, questions):
    vocabulary._verify(c, words, snapshot.vocabulary.schema)
    if _schema(c) != snapshot.schema or _rows(c) != questions:
        raise TransferError("등록된 단어 문제 구조 또는 전체 내용이 예상 결과와 다릅니다.")
    sequence = _sequence_state(c)
    if sequence["last_value"] + int(sequence["is_called"]) <= max(row["question_id"] for row in questions):
        raise TransferError("단어 문제의 다음 자동 생성 번호를 확인하지 못했습니다.")


def execute_question_transfer(source_url, target_url, plan, root):
    fresh = capture_questions(source_url)
    if fresh.signature != plan.snapshot.signature:
        raise TransferError("미리보기 이후 원본 단어·문제가 변경되었습니다. 등록 내용을 다시 확인하세요.")
    backup, commit_attempted, committed = None, False, False
    try:
        with connection(target_url, readonly=False) as c:
            if _distinct_target(c, fresh.vocabulary.identity) != plan.vocabulary.target_identity:
                raise TransferError("대상 DB가 변경되었습니다. 등록 내용을 다시 확인하세요.")
            for lock in ("schema_migrations", "vocabulary_import"):
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("topik_bank:" + lock,))
            word_schema, schema = vocabulary._schema(c), _schema(c)
            # Use the same word-before-question lock order as editing and generation.
            if word_schema:
                c.execute("LOCK TABLE topik_bank.vocabulary IN SHARE ROW EXCLUSIVE MODE")
            if schema:
                c.execute("LOCK TABLE topik_bank.vocabulary_questions IN SHARE ROW EXCLUSIVE MODE")
            word_schema, schema = vocabulary._schema(c), _schema(c)
            words = vocabulary._rows(c) if word_schema else []
            previous = _rows(c) if schema else []
            if (digest((word_schema, words)) != plan.vocabulary.target_signature
                    or digest((schema, previous)) != plan.target_signature):
                raise TransferError("미리보기 이후 대상 단어·문제가 변경되었습니다. 등록 내용을 다시 확인하세요.")
            effects = _effects(fresh, words, previous)
            word_effects = vocabulary._effects(fresh.vocabulary.rows, words)
            if effects["conflicts"] or word_effects["conflicts"]:
                raise TransferError("단어·문제 충돌을 해결한 후 다시 확인하세요.")
            word_sequence = vocabulary._sequence_state(c) if word_schema else None
            sequence = _sequence_state(c) if schema else None
            word_next = word_sequence["last_value"] + int(word_sequence["is_called"]) if word_sequence else 1
            next_id = max(word_next, max(row["vocabulary_id"] for row in words + fresh.vocabulary.rows) + 1)
            question_next = sequence["last_value"] + int(sequence["is_called"]) if sequence else 1
            next_question_id = max(question_next, max((row["question_id"] for row in previous), default=0) + 1)
            changes = (word_schema is None or schema is None or plan.vocabulary.upgrade_audio
                       or plan.vocabulary.upgrade_languages or word_effects["created"] or word_effects["updated"]
                       or effects["created"] or effects["updated"] or next_id != word_next or next_question_id != question_next)
            if changes:
                if word_schema or schema:
                    backup = _backup(root, plan.vocabulary.target_identity, word_schema, words, word_sequence,
                                     schema, previous, sequence)
                words = _write_words(c, fresh.vocabulary, word_schema, words, next_id)
                if schema is None:
                    c.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
                    if c.execute("SELECT to_regclass('topik_bank.schema_migrations')").fetchone()[0]:
                        c.execute("INSERT INTO topik_bank.schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
                                  (SCHEMA_FILE.stem,))
                if effects["created"] or effects["updated"] or next_question_id != question_next:
                    # Transactional RESTART also rolls back nextval calls on a failed transfer.
                    c.execute(sql.SQL("ALTER TABLE topik_bank.vocabulary_questions ALTER COLUMN question_id RESTART WITH {}")
                              .format(sql.Literal(next_question_id)))
                questions = _write_questions(c, fresh.questions, previous)
            else:
                questions = previous
            _verify(c, fresh, words, questions)
            commit_attempted = True
        committed = True
        with connection(target_url) as c:
            if _distinct_target(c, fresh.vocabulary.identity) != plan.vocabulary.target_identity:
                raise TransferError("대상 DB가 변경되었습니다.")
            _verify(c, fresh, words, questions)
        return {"status": "verified", "created": effects["created"], "updated": effects["updated"],
                "unchanged": effects["unchanged"], "extra": effects["extra"], "source_count": len(fresh.questions),
                "target_count": len(questions), "word_count": len(fresh.vocabulary.rows), "excluded": len(fresh.excluded),
                "backup": str(backup) if backup else None}
    except Exception as exc:
        if committed:
            message = "등록은 커밋되었지만 최종 조회 검증을 완료하지 못했습니다. 등록 내용을 다시 확인하세요."
        elif commit_attempted:
            message = "커밋 결과를 확정하지 못했습니다. 등록 내용을 다시 확인하세요."
        else:
            message = str(exc) if isinstance(exc, TransferError) else "단어 문제 등록에 실패해 변경을 롤백했습니다."
        if backup:
            message += f" 백업: {backup}"
        raise TransferError(message) from None
