from contextlib import contextmanager
import json
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from test_vocabulary_questions import CONFIG, bank, ready_task
from topik_question_lab.common.vocabulary.vocabulary_models import source_snapshot
from topik_question_lab.menus.vocabulary_transfer import vocabulary_question_transfer as transfer
from topik_question_lab.menus.vocabulary_transfer import vocabulary_transfer as vocabulary


PAGE = Path(__file__).resolve().parents[1] / "topik_question_lab/menus/vocabulary_transfer/vocabulary_transfer_app.py"


@pytest.fixture
def banks(bank, postgres_database_factory):
    bank.save(ready_task(bank), CONFIG, bank.identity())
    return bank.url, postgres_database_factory()


def state(url):
    with transfer.connection(url) as c:
        word_schema, schema = vocabulary._schema(c), transfer._schema(c)
        return {
            "words": vocabulary._rows(c) if word_schema else None,
            "questions": transfer._rows(c) if schema else None,
            "word_sequence": vocabulary._sequence_state(c) if word_schema else None,
            "sequence": transfer._sequence_state(c) if schema else None,
        }


def prepare(source, target):
    return transfer.prepare_question_transfer(transfer.capture_questions(source), target)


def send(source, target, root):
    return transfer.execute_question_transfer(source, target, prepare(source, target), root)


def change_source(source):
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET english='updated meaning' WHERE vocabulary_id=1")
        word = vocabulary._rows(c)[0]
        c.execute("UPDATE topik_bank.vocabulary_questions SET source_hash=%s", (transfer.digest(source_snapshot(word)),))
        c.execute("UPDATE topik_bank.vocabulary_questions SET explanation='수정 해설' WHERE position=1")


def test_preview_first_registration_and_unrelated_data(banks, tmp_path):
    source, target = banks
    before = state(source)
    with psycopg.connect(target) as c:
        c.execute("CREATE TABLE public.unrelated(value TEXT)")
        c.execute("INSERT INTO public.unrelated VALUES ('preserve')")
    plan = prepare(source, target)
    assert plan.create_table and plan.vocabulary.create_table
    assert plan.effects == {"created": 7, "updated": 0, "unchanged": 0, "deleted": 0}
    assert state(target)["questions"] is None and not list(tmp_path.rglob("*"))
    result = transfer.execute_question_transfer(source, target, plan, tmp_path)
    assert result["status"] == "verified" and result["created"] == 7 and result["word_count"] == 1
    assert result["backup"] is None
    after = state(target)
    assert state(source) == before
    assert after["words"] == before["words"]
    assert [transfer._content(q) for q in after["questions"]] == [transfer._content(q) for q in before["questions"]]
    with psycopg.connect(target) as c:
        assert c.execute("SELECT * FROM public.unrelated").fetchall() == [("preserve",)]
        assert {row[0] for row in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='topik_bank'")} == {
            "vocabulary", "vocabulary_questions"}


def test_retry_is_noop_and_does_not_allocate_ids_or_backup(banks, tmp_path, monkeypatch):
    source, target = banks
    send(source, target, tmp_path)
    before = state(target)
    monkeypatch.setattr(transfer, "_backup", lambda *a: pytest.fail("no backup for identical content"))
    result = send(source, target, tmp_path)
    assert (result["created"], result["updated"], result["unchanged"]) == (0, 0, 7)
    assert state(target) == before


@pytest.mark.parametrize("existing", [False, True])
def test_audio_language_columns_and_metadata_transfer_together(banks, tmp_path, existing):
    source, target = banks
    if existing:
        send(source, target, tmp_path)
    with psycopg.connect(source) as c:
        vocabulary.apply_language_migration(c)
        vocabulary.apply_audio_migration(c)
        c.execute("""UPDATE topik_bank.vocabulary SET chinese_simplified='推迟',indonesian='menunda',
            audio_storage_bucket='test-bucket',audio_storage_path='media/test.mp3',
            audio_file_sha256=repeat('a',64),audio_byte_size=123,audio_duration_ms=1000,
            audio_source_hash=repeat('b',64),audio_generation_config='{}'::jsonb,audio_created_at=CURRENT_TIMESTAMP""")
        word = vocabulary._rows(c)[0]
        c.execute("UPDATE topik_bank.vocabulary_questions SET source_hash=%s", (transfer.digest(source_snapshot(word)),))
    plan = prepare(source, target)
    assert plan.vocabulary.upgrade_audio is existing and plan.vocabulary.upgrade_languages is existing
    transfer.execute_question_transfer(source, target, plan, tmp_path)
    assert state(target)["words"] == state(source)["words"]
    assert send(source, target, tmp_path)["unchanged"] == 7
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET " + ",".join(f"{key}=NULL" for key in vocabulary.AUDIO_FIELDS))
    before = state(target)
    assert send(source, target, tmp_path)["unchanged"] == 7
    assert state(target)["words"] == state(source)["words"]
    assert state(target)["questions"] == before["questions"]
    assert all(state(target)["words"][0][key] is None for key in vocabulary.AUDIO_FIELDS)


def test_failed_upgrade_rolls_back_columns_and_migration_record(banks, tmp_path, monkeypatch):
    source, target = banks
    vocabulary.execute_vocabulary_transfer(source, target,
        vocabulary.prepare_vocabulary_transfer(vocabulary.capture_vocabulary(source), target), tmp_path)
    with psycopg.connect(target) as c:
        c.execute("CREATE TABLE topik_bank.schema_migrations(version TEXT PRIMARY KEY)")
    with psycopg.connect(source) as c:
        vocabulary.apply_language_migration(c)
        vocabulary.apply_audio_migration(c)
    before = state(target)

    def fail(*args):
        raise RuntimeError("verification failure")

    monkeypatch.setattr(transfer, "_verify", fail)
    with pytest.raises(transfer.TransferError, match="롤백"):
        send(source, target, tmp_path)
    assert state(target) == before
    with psycopg.connect(target) as c:
        assert c.execute("SELECT * FROM topik_bank.schema_migrations").fetchall() == []


def test_updates_preserve_target_ids_references_and_backup_both_tables(banks, tmp_path):
    source, target = banks
    with psycopg.connect(target) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute(vocabulary.SCHEMA_FILE.read_text(encoding="utf-8"))
        c.execute(transfer.SCHEMA_FILE.read_text(encoding="utf-8"))
        c.execute("ALTER TABLE topik_bank.vocabulary_questions ALTER COLUMN question_id RESTART WITH 100")
    send(source, target, tmp_path)
    before = state(target)
    with psycopg.connect(target) as c:
        c.execute("CREATE TABLE public.question_reference(question_id BIGINT REFERENCES topik_bank.vocabulary_questions)")
        c.execute("INSERT INTO public.question_reference VALUES (100)")
    change_source(source)
    plan = prepare(source, target)
    assert plan.effects["updated"] == 7 and plan.vocabulary.effects["updated"] == 1
    result = transfer.execute_question_transfer(source, target, plan, tmp_path)
    after = state(target)
    assert [q["question_id"] for q in after["questions"]] == list(range(100, 107))
    assert after["questions"][0]["explanation"] == "수정 해설"
    assert after["words"] == state(source)["words"]
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    assert backup["tables"]["vocabulary"]["rows"] == before["words"]
    assert backup["tables"]["vocabulary_questions"]["rows"] == before["questions"]
    assert backup["tables"]["vocabulary_questions"]["sequence"] == before["sequence"]
    assert backup["content_hash"] == transfer.digest(backup["tables"])
    with psycopg.connect(target) as c:
        assert c.execute("SELECT * FROM public.question_reference").fetchall() == [(100,)]


def test_target_only_word_and_questions_are_deleted_with_backup(banks, tmp_path):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(target) as c:
        extra = {**vocabulary._rows(c)[0], "vocabulary_id": 40, "korean": "추가 단어"}
        vocabulary._upsert(c, [extra])
        c.execute("""INSERT INTO topik_bank.vocabulary_questions
            (vocabulary_id,question_type,position,instruction,stem,choices,correct_answer,accepted_answers,
             explanation,provider,model,generation_id,source_hash)
            SELECT 40,question_type,position,instruction,stem,choices,correct_answer,accepted_answers,
             explanation,provider,model,generation_id,source_hash FROM topik_bank.vocabulary_questions WHERE vocabulary_id=1""")
    before = state(target)
    change_source(source)
    result = send(source, target, tmp_path)
    assert result["deleted"] == 7 and result["deleted_words"] == 1
    assert state(target)["words"] == state(source)["words"]
    assert all(q["vocabulary_id"] == 1 for q in state(target)["questions"])
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    assert backup["tables"]["vocabulary"]["rows"] == before["words"]
    assert backup["tables"]["vocabulary_questions"]["rows"] == before["questions"]
    assert state(target)["word_sequence"]["last_value"] >= 41


@pytest.mark.parametrize("change", ["missing", "partial", "stale", "invalid"])
def test_invalid_bundles_are_excluded_and_server_questions_are_removed(banks, tmp_path, change):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(source) as c:
        if change == "missing":
            c.execute("DELETE FROM topik_bank.vocabulary_questions")
        elif change == "partial":
            c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE position=2")
        elif change == "stale":
            c.execute("UPDATE topik_bank.vocabulary SET english='changed'")
        else:
            c.execute("""UPDATE topik_bank.vocabulary_questions SET choices='["a","a","a","a"]'::jsonb WHERE position=1""")
    snapshot = transfer.capture_questions(source)
    assert not snapshot.questions and len(snapshot.vocabulary.rows) == 1
    assert len(snapshot.excluded) == 1 and snapshot.excluded[0]["사유"]
    plan = transfer.prepare_question_transfer(snapshot, target)
    assert plan.effects["deleted"] == 7
    result = transfer.execute_question_transfer(source, target, plan, tmp_path)
    assert result["deleted"] == 7 and result["word_count"] == 1
    assert state(target)["words"] == state(source)["words"] and state(target)["questions"] == []
    assert send(source, target, tmp_path)["backup"] is None


def test_all_words_and_only_ready_bundles_are_transferred(banks, tmp_path):
    source, target = banks
    with psycopg.connect(source) as c:
        vocabulary._upsert(c, [{**vocabulary._rows(c)[0], "vocabulary_id": 2, "korean": "미생성"}])
    result = send(source, target, tmp_path)
    assert result["excluded"] == 1 and result["word_count"] == 2
    assert [w["vocabulary_id"] for w in state(target)["words"]] == [1, 2]
    assert all(q["vocabulary_id"] == 1 for q in state(target)["questions"])


@pytest.mark.parametrize("collision", ["word", "extra_question", "preserved_meaning"])
def test_word_changes_extra_questions_and_empty_meanings_sync(banks, tmp_path, collision):
    source, target = banks
    send(source, target, tmp_path)
    if collision == "word":
        with psycopg.connect(target) as c:
            c.execute("UPDATE topik_bank.vocabulary SET korean='다른 단어'")
    elif collision == "extra_question":
        with psycopg.connect(source) as c:
            c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE position=7")
    else:
        for url in (source, target):
            with psycopg.connect(url) as c:
                vocabulary.apply_language_migration(c)
        with psycopg.connect(target) as c:
            c.execute("UPDATE topik_bank.vocabulary SET chinese_simplified='existing meaning'")
    before = state(target)
    plan = prepare(source, target)
    result = transfer.execute_question_transfer(source, target, plan, tmp_path)
    after = state(target)
    assert after["words"] == state(source)["words"]
    assert [transfer._content(q) for q in after["questions"]] == [transfer._content(q) for q in state(source)["questions"]]
    assert [q["question_id"] for q in after["questions"]] == [q["question_id"] for q in before["questions"][:len(after["questions"])]]
    assert result["deleted"] == int(collision == "extra_question")


@pytest.fixture
def extra_target(banks, tmp_path):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(target) as c:
        extra = {**vocabulary._rows(c)[0], "vocabulary_id": 40, "korean": "서버 전용"}
        vocabulary._upsert(c, [extra])
        fields = ','.join(transfer.COPY_FIELDS)
        values = ','.join(transfer.COPY_FIELDS[1:])
        c.execute(f"INSERT INTO topik_bank.vocabulary_questions({fields}) "
                  f"SELECT 40,{values} FROM topik_bank.vocabulary_questions WHERE vocabulary_id=1")
    return source, target


def test_words_only_sync_backs_up_and_deletes_children_of_removed_words(extra_target, tmp_path):
    source, target = extra_target
    before = state(target)
    plan = vocabulary.prepare_vocabulary_transfer(vocabulary.capture_vocabulary(source), target)
    assert plan.effects["deleted"] == 1 and plan.deleted_questions == 7
    result = vocabulary.execute_vocabulary_transfer(source, target, plan, tmp_path)
    assert result["deleted"] == 1 and result["deleted_questions"] == 7
    after = state(target)
    assert after["words"] == state(source)["words"]
    assert after["questions"] == [q for q in before["questions"] if q["vocabulary_id"] == 1]
    assert after["sequence"] == before["sequence"]
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    assert backup["tables"]["vocabulary"]["rows"] == before["words"]
    assert backup["tables"]["vocabulary_questions"]["rows"] == before["questions"]
    assert vocabulary.execute_vocabulary_transfer(source, target,
        vocabulary.prepare_vocabulary_transfer(vocabulary.capture_vocabulary(source), target), tmp_path)["backup"] is None


@pytest.mark.parametrize("change", ["edit", "drop"])
def test_words_only_sync_rejects_changed_question_preview(extra_target, tmp_path, change):
    source, target = extra_target
    plan = vocabulary.prepare_vocabulary_transfer(vocabulary.capture_vocabulary(source), target)
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.vocabulary_questions SET explanation='최신 수정' WHERE vocabulary_id=40"
                  if change == "edit" else "DROP TABLE topik_bank.vocabulary_questions")
    before = state(target)
    with pytest.raises(transfer.TransferError, match="대상 문제"):
        vocabulary.execute_vocabulary_transfer(source, target, plan, tmp_path)
    assert state(target) == before


@pytest.mark.parametrize("words_only", [False, True])
@pytest.mark.parametrize("failure", ["backup", "verification", "reference"])
def test_deletion_failures_roll_back_words_questions_and_sequences(extra_target, tmp_path, monkeypatch, words_only, failure):
    source, target = extra_target
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.vocabulary SET part_of_speech='명사' WHERE vocabulary_id=1")
        if failure == "reference":
            c.execute("CREATE TABLE public.question_reference(question_id BIGINT REFERENCES topik_bank.vocabulary_questions)")
            c.execute("INSERT INTO public.question_reference SELECT question_id FROM topik_bank.vocabulary_questions "
                      "WHERE vocabulary_id=40 AND position=1")
    before = state(target)
    if failure != "reference":
        def fail(*args):
            raise RuntimeError("synthetic-secret")
        owner = vocabulary if words_only and failure == "verification" else transfer
        monkeypatch.setattr(owner, "_backup" if failure == "backup" else "_verify", fail)
    expected_error = "SQLSTATE 23503" if failure == "reference" else "롤백"
    with pytest.raises(transfer.TransferError, match=expected_error) as error:
        if words_only:
            vocabulary.execute_vocabulary_transfer(source, target,
                vocabulary.prepare_vocabulary_transfer(vocabulary.capture_vocabulary(source), target), tmp_path)
        else:
            send(source, target, tmp_path)
    assert "synthetic-secret" not in str(error.value)
    assert state(target) == before
    if failure == "reference":
        with psycopg.connect(target) as c:
            assert c.execute("SELECT count(*) FROM public.question_reference").fetchone()[0] == 1


def test_noun_correction_removes_conjugation_questions_but_keeps_remaining_ids(banks, tmp_path):
    source, target = banks
    send(source, target, tmp_path)
    before = state(target)
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET korean='정세',part_of_speech='명사'")
        c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE position>3")
        c.execute("UPDATE topik_bank.vocabulary_questions SET source_hash=%s",
                  (transfer.digest(source_snapshot(vocabulary._rows(c)[0])),))
    plan = prepare(source, target)
    assert plan.vocabulary.effects["updated"] == 1 and plan.effects["deleted"] == 4
    result = transfer.execute_question_transfer(source, target, plan, tmp_path)
    after = state(target)
    assert result["deleted"] == 4 and after["words"] == state(source)["words"]
    assert [q["question_id"] for q in after["questions"]] == [q["question_id"] for q in before["questions"][:3]]
    assert transfer.question_status({"word": source_snapshot(after["words"][0]), "existing": after["questions"]}) == "ready"


def test_ui_previews_full_sync_deletions_and_overwrites_changed_pos(extra_target, monkeypatch):
    source, target = extra_target
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.vocabulary SET part_of_speech='명사' WHERE vocabulary_id=1")
    monkeypatch.setenv("DATABASE_URL", source)
    monkeypatch.setenv("QUESTION_BANK_LOCAL_DATABASE_URL", target)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.radio[0].set_value("단어 연습문제").run()
    app.button[0].click().run()
    assert not app.exception and not app.error
    word_counts = next(df.value for df in app.dataframe if "현재 단어" in df.value.columns)
    question_counts = next(df.value for df in app.dataframe if "현재 문제" in df.value.columns)
    assert word_counts.iloc[0]["변경"] == word_counts.iloc[0]["삭제"] == 1
    assert question_counts.iloc[0]["삭제"] == 7
    button = next(b for b in app.button if b.label == "선택 서버에 단어 문제 등록")
    assert not button.disabled
    button.click().run()
    assert not app.exception and any("삭제 7" in notice.value for notice in app.success)
    assert state(target)["words"] == state(source)["words"]
    assert len(state(target)["questions"]) == 7


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("part", ["word", "question", "schema"])
def test_preview_changes_are_rejected(banks, tmp_path, side, part):
    source, target = banks
    send(source, target, tmp_path)
    plan = prepare(source, target)
    with psycopg.connect(source if side == "source" else target) as c:
        if part == "word":
            c.execute("UPDATE topik_bank.vocabulary SET english='concurrent edit'")
        elif part == "question":
            c.execute("UPDATE topik_bank.vocabulary_questions SET explanation='concurrent edit' WHERE position=1")
        else:
            c.execute("ALTER TABLE topik_bank.vocabulary_questions ADD CONSTRAINT extra_check CHECK(length(stem)<10000)")
    before = state(target)
    with pytest.raises(transfer.TransferError, match="변경"):
        transfer.execute_question_transfer(source, target, plan, tmp_path)
    assert state(target) == before


@pytest.mark.parametrize("existing", [False, True])
def test_verification_failure_rolls_back_words_questions_schema_and_sequences(banks, tmp_path, monkeypatch, existing):
    source, target = banks
    if existing:
        send(source, target, tmp_path)
        change_source(source)
    before = state(target)

    def fail(*args):
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(transfer, "_verify", fail)
    with pytest.raises(transfer.TransferError, match="롤백") as error:
        send(source, target, tmp_path)
    assert "synthetic-secret" not in str(error.value)
    assert state(target) == before


def test_backup_failure_prevents_any_changes(banks, tmp_path, monkeypatch):
    source, target = banks
    send(source, target, tmp_path)
    change_source(source)
    before = state(target)

    def fail(*args):
        raise OSError("synthetic-secret")

    monkeypatch.setattr(transfer, "_backup", fail)
    with pytest.raises(transfer.TransferError) as error:
        send(source, target, tmp_path)
    assert "synthetic-secret" not in str(error.value)
    assert state(target) == before


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE topik_bank.vocabulary_questions ADD COLUMN unexpected TEXT",
    "ALTER TABLE topik_bank.vocabulary_questions ALTER COLUMN stem DROP NOT NULL",
    "ALTER TABLE topik_bank.vocabulary_questions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE topik_bank.vocabulary_questions DROP CONSTRAINT vocabulary_questions_vocabulary_id_position_key",
    "CREATE TRIGGER custom BEFORE INSERT ON topik_bank.vocabulary_questions FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
])
def test_incompatible_question_schema_is_rejected(banks, tmp_path, ddl):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(target) as c:
        c.execute(ddl)
    with pytest.raises(transfer.TransferError):
        prepare(source, target)


def test_source_and_replacement_destinations_are_rejected(banks, postgres_database_factory, tmp_path):
    source, target = banks
    with pytest.raises(transfer.TransferError, match="원본"):
        prepare(source, source)
    plan = prepare(source, target)
    with pytest.raises(transfer.TransferError, match="대상 DB"):
        transfer.execute_question_transfer(source, postgres_database_factory(), plan, tmp_path)


def test_post_commit_verification_failure_requires_reinspection(banks, tmp_path, monkeypatch):
    source, target = banks
    original, calls = transfer._verify, []

    def fail_second(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("synthetic-secret")
        original(*args)

    monkeypatch.setattr(transfer, "_verify", fail_second)
    with pytest.raises(transfer.TransferError, match="커밋되었지만"):
        send(source, target, tmp_path)
    assert len(state(target)["questions"]) == 7


def test_lost_commit_ack_requires_reinspection(banks, tmp_path, monkeypatch):
    source, target = banks
    original = transfer.connection

    @contextmanager
    def lost_ack(url, *, readonly=True):
        with original(url, readonly=readonly) as c:
            yield c
        if not readonly:
            raise transfer.TransferError("synthetic-secret")

    monkeypatch.setattr(transfer, "connection", lost_ack)
    with pytest.raises(transfer.TransferError, match="커밋 결과를 확정하지 못했습니다"):
        send(source, target, tmp_path)
    assert len(state(target)["questions"]) == 7


def test_ui_partial_failure_retry_and_mode_change(banks, postgres_database_factory, monkeypatch):
    source, local = banks
    production = postgres_database_factory()
    for key, value in (("DATABASE_URL", source), ("QUESTION_BANK_LOCAL_DATABASE_URL", local),
                       ("QUESTION_BANK_PRODUCTION_DATABASE_URL", production)):
        monkeypatch.setenv(key, value)
    original, calls = transfer.execute_question_transfer, []

    def fail_once(source_url, target_url, plan, root):
        calls.append(target_url)
        if target_url == production and calls.count(production) == 1:
            raise transfer.TransferError("테스트 연결 실패")
        return original(source_url, target_url, plan, root)

    monkeypatch.setattr(transfer, "execute_question_transfer", fail_once)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.radio[0].set_value("단어 연습문제").run()
    app.multiselect[0].set_value(["로컬 서버", "운영 서버"]).run()
    app.button[0].click().run()
    assert not app.exception and state(local)["questions"] is None
    next(b for b in app.button if b.label == "선택 서버에 단어 문제 등록").click().run()
    assert not app.exception and any("운영 서버: 테스트 연결 실패" in s.value for s in app.warning)
    assert len(state(local)["questions"]) == 7 and state(production)["questions"] is None
    app.multiselect[0].set_value(["운영 서버"]).run()
    app.button[0].click().run()
    next(b for b in app.button if b.label == "선택 서버에 단어 문제 등록").click().run()
    assert not app.exception and calls == [local, production, production]
    assert len(state(production)["questions"]) == 7
    app.radio[0].set_value("단어장").run()
    assert not app.exception and not app.success
    assert not any(b.label == "선택 서버에 단어 문제 등록" for b in app.button)


def test_ui_exclusions_allow_sync_and_duplicate_destinations_block_execution(banks, monkeypatch):
    source, target = banks
    for key, value in (("DATABASE_URL", source), ("QUESTION_BANK_LOCAL_DATABASE_URL", target),
                       ("QUESTION_BANK_PRODUCTION_DATABASE_URL", target)):
        monkeypatch.setenv(key, value)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.radio[0].set_value("단어 연습문제").run()
    app.multiselect[0].set_value(["로컬 서버", "운영 서버"]).run()
    app.button[0].click().run()
    assert not app.exception and any("같은 대상 DB" in e.value for e in app.error)
    assert next(b for b in app.button if b.label == "선택 서버에 단어 문제 등록").disabled
    with psycopg.connect(source) as c:
        c.execute("DELETE FROM topik_bank.vocabulary_questions WHERE position=1")
    app.multiselect[0].set_value(["로컬 서버"]).run()
    app.button[0].click().run()
    assert not app.exception and not app.error
    assert any("해당 단어의 기존 서버 문제도 삭제" in caption.value for caption in app.caption)
    assert not next(b for b in app.button if b.label == "선택 서버에 단어 문제 등록").disabled
    assert state(target)["questions"] is None


def test_unconfigured_questions_page_never_connects(monkeypatch):
    monkeypatch.setattr(transfer, "capture_questions", lambda *a: pytest.fail("must not connect"))
    app = AppTest.from_file(str(PAGE)).run()
    app.radio[0].set_value("단어 연습문제").run()
    assert not app.exception and app.button[0].disabled
