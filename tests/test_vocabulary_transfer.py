from contextlib import contextmanager
import json
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from test_vocabulary import SAMPLE
from topik_question_lab.step_06_publication import vocabulary_transfer as transfer


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "topik_question_lab/ui/vocabulary_transfer_app.py"


def stored(url):
    with transfer.connection(url) as c:
        return transfer._rows(c) if transfer._schema(c) else None


def sequence(url):
    with transfer.connection(url) as c:
        return transfer._sequence_state(c)


def prepare(source, target):
    return transfer.prepare_vocabulary_transfer(transfer.capture_vocabulary(source), target)


def send(source, target, root):
    return transfer.execute_vocabulary_transfer(source, target, prepare(source, target), root)


@pytest.fixture
def banks(postgres_database_factory):
    source, target = postgres_database_factory(), postgres_database_factory()
    with psycopg.connect(source) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute(transfer.SCHEMA_FILE.read_text(encoding="utf-8"))
        with c.cursor() as cursor:
            cursor.executemany("""INSERT INTO topik_bank.vocabulary
                (korean,part_of_speech,english,vietnamese,japanese,example_korean,example_english,topik_level)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", SAMPLE)
    return source, target


def test_preview_is_readonly_and_first_transfer_preserves_ids_and_other_tables(banks, tmp_path):
    source, target = banks
    before = stored(source)
    with psycopg.connect(target) as c:
        c.execute("CREATE TABLE public.unrelated(value TEXT)")
        c.execute("INSERT INTO public.unrelated VALUES ('preserve')")
    plan = prepare(source, target)
    assert plan.create_table and plan.target_count == 0
    assert plan.effects == {"created": 4, "updated": 0, "unchanged": 0, "conflicts": [], "extra": 0}
    assert plan.snapshot.levels == {3: 1, 4: 1, 5: 1, 6: 1}
    assert stored(target) is None and not list(tmp_path.rglob("*"))
    result = transfer.execute_vocabulary_transfer(source, target, plan, tmp_path)
    assert result["status"] == "verified" and result["created_table"] and result["created"] == 4
    assert result["backup"] is None
    assert stored(source) == before == stored(target)
    with psycopg.connect(target) as c:
        assert c.execute("SELECT * FROM public.unrelated").fetchall() == [("preserve",)]
        assert c.execute("SELECT tablename FROM pg_tables WHERE schemaname='topik_bank'").fetchall() == [("vocabulary",)]


def test_retry_is_noop_and_identity_sequence_is_usable(banks, tmp_path, monkeypatch):
    source, target = banks
    send(source, target, tmp_path)
    before, seq = stored(target), sequence(target)
    monkeypatch.setattr(transfer, "_backup", lambda *a: pytest.fail("unchanged data needs no backup"))
    result = send(source, target, tmp_path)
    assert (result["created"], result["updated"], result["unchanged"]) == (0, 0, 4)
    assert stored(target) == before and sequence(target) == seq
    with psycopg.connect(target) as c:
        new_id = c.execute("""INSERT INTO topik_bank.vocabulary
            (korean,english,vietnamese,japanese,example_korean,example_english,topik_level)
            VALUES ('추가','extra','extra','extra','예문','example',3) RETURNING vocabulary_id""").fetchone()[0]
    assert new_id > max(row["vocabulary_id"] for row in before)


@pytest.mark.parametrize("reverse_order", [False, True])
def test_schema_matches_legacy_catalog_and_sort_order(banks, reverse_order):
    source, _ = banks

    class LegacyCatalog:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, *args):
            cursor = self.connection.execute(query, *args)
            if "SELECT pg_get_constraintdef(oid),convalidated" not in query:
                return cursor
            # PostgreSQL 16/17 keep column NOT NULL only in pg_attribute.
            rows = [row for row in cursor.fetchall() if not row[0].startswith("NOT NULL ")]
            if reverse_order:
                rows.reverse()

            class Constraints:
                def fetchall(self):
                    return rows

            return Constraints()

    with transfer.connection(source) as c:
        current = transfer._schema(c)
        legacy = transfer._schema(LegacyCatalog(c))
    assert current == legacy
    assert all(column[2] for column in current["columns"])


def test_updates_preserve_target_only_words_and_write_verified_backup(banks, tmp_path):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(target) as c:
        extra = {**stored(source)[0], "vocabulary_id": 500, "korean": "대상 전용"}
        transfer._upsert(c, [extra])
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET english='updated meaning' WHERE vocabulary_id=1")
        transfer._upsert(c, [{**extra, "vocabulary_id": 10, "korean": "원본 추가"}])
    before, seq = stored(target), sequence(target)
    result = send(source, target, tmp_path)
    assert (result["created"], result["updated"], result["unchanged"], result["extra"]) == (1, 1, 3, 1)
    assert stored(target)[-1] == extra
    assert sequence(target)["last_value"] == 501 and not sequence(target)["is_called"]
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    assert backup["rows"] == before and backup["sequence"] == seq
    assert backup["content_hash"] == transfer.digest((backup["schema"], before, seq))
    assert "postgresql://" not in Path(result["backup"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("change", ["korean='다른 단어'", "part_of_speech='동사'", "vocabulary_id=99"])
def test_id_and_natural_key_conflicts_block_before_backup(banks, tmp_path, monkeypatch, change):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(target) as c:
        if change == "vocabulary_id=99":
            row = c.execute("SELECT to_jsonb(v) FROM topik_bank.vocabulary v WHERE vocabulary_id=1").fetchone()[0]
            c.execute("DELETE FROM topik_bank.vocabulary WHERE vocabulary_id=1")
            transfer._upsert(c, [{**row, "vocabulary_id": 99}])
        else:
            c.execute("UPDATE topik_bank.vocabulary SET " + change + " WHERE vocabulary_id=1")
    before = stored(target)
    plan = prepare(source, target)
    assert len(plan.effects["conflicts"]) == 1
    monkeypatch.setattr(transfer, "_backup", lambda *a: pytest.fail("must reject before backup"))
    with pytest.raises(transfer.TransferError, match="충돌"):
        transfer.execute_vocabulary_transfer(source, target, plan, tmp_path)
    assert stored(target) == before


@pytest.mark.parametrize("side", ["source", "target", "schema"])
def test_stale_preview_is_rejected(banks, tmp_path, side):
    source, target = banks
    send(source, target, tmp_path)
    plan = prepare(source, target)
    with psycopg.connect(source if side == "source" else target) as c:
        if side == "schema":
            c.execute("ALTER TABLE topik_bank.vocabulary ADD CONSTRAINT test_length CHECK(length(korean)<1000)")
        else:
            c.execute("UPDATE topik_bank.vocabulary SET english='concurrent change' WHERE vocabulary_id=1")
    before = stored(target)
    with pytest.raises(transfer.TransferError, match="변경"):
        transfer.execute_vocabulary_transfer(source, target, plan, tmp_path)
    assert stored(target) == before
    assert not list((tmp_path / "backups").rglob("*.json"))


def test_source_destination_and_target_switch_are_blocked(banks, postgres_database_factory, tmp_path):
    source, target = banks
    with pytest.raises(transfer.TransferError, match="원본"):
        prepare(source, source)
    plan = prepare(source, target)
    with pytest.raises(transfer.TransferError, match="원본"):
        transfer.execute_vocabulary_transfer(source, source, plan, tmp_path)
    replacement = postgres_database_factory()
    with pytest.raises(transfer.TransferError, match="대상 DB"):
        transfer.execute_vocabulary_transfer(source, replacement, plan, tmp_path)
    assert stored(target) is None and stored(replacement) is None


@pytest.mark.parametrize("existing", [False, True])
def test_verification_failure_rolls_back_rows_schema_and_sequence(banks, tmp_path, monkeypatch, existing):
    source, target = banks
    if existing:
        send(source, target, tmp_path)
    with psycopg.connect(source) as c:
        transfer._upsert(c, [{**stored(source)[0], "vocabulary_id": 100, "korean": "새 단어"}])
    before, seq = stored(target), sequence(target) if existing else None

    def fail(*args):
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(transfer, "_verify", fail)
    with pytest.raises(transfer.TransferError) as error:
        send(source, target, tmp_path)
    assert "synthetic-secret" not in str(error.value)
    assert stored(target) == before
    if existing:
        assert sequence(target) == seq
    else:
        with psycopg.connect(target) as c:
            assert c.execute("SELECT to_regnamespace('topik_bank')").fetchone()[0] is None


def test_backup_failure_prevents_updates(banks, tmp_path, monkeypatch):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET english='new' WHERE vocabulary_id=1")
    before = stored(target)

    def fail(*args):
        raise OSError("synthetic-secret")

    monkeypatch.setattr(transfer, "_backup", fail)
    with pytest.raises(transfer.TransferError) as error:
        send(source, target, tmp_path)
    assert "synthetic-secret" not in str(error.value)
    assert stored(target) == before


def test_post_commit_verification_failure_is_not_reported_as_rollback(banks, tmp_path, monkeypatch):
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
    assert stored(target) == stored(source)


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
    assert stored(target) == stored(source)


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE topik_bank.vocabulary ADD COLUMN unexpected TEXT",
    "ALTER TABLE topik_bank.vocabulary ALTER COLUMN english DROP NOT NULL",
    "ALTER TABLE topik_bank.vocabulary ENABLE ROW LEVEL SECURITY",
    "CREATE TRIGGER test_trigger BEFORE INSERT ON topik_bank.vocabulary FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
])
def test_incompatible_target_is_rejected(banks, tmp_path, ddl):
    source, target = banks
    send(source, target, tmp_path)
    with psycopg.connect(target) as c:
        c.execute(ddl)
    with pytest.raises(transfer.TransferError):
        prepare(source, target)


def test_unconfigured_page_and_main_menu_do_not_start_workers(tmp_path, monkeypatch):
    from topik_question_lab.ui import runner_service

    monkeypatch.setattr(transfer, "capture_vocabulary", lambda *a: pytest.fail("must not connect"))
    app = AppTest.from_file(str(PAGE)).run()
    assert not app.exception and app.button[0].disabled
    assert not list(tmp_path.rglob("*"))
    (tmp_path / "data").mkdir()
    (tmp_path / "data/bulk_generation.db").touch()
    monkeypatch.setattr(runner_service, "get_runner", lambda *a: pytest.fail("must not recover a worker"))
    main = AppTest.from_file(str(ROOT / "topik_question_lab/main.py"))
    main.switch_page("ui/vocabulary_transfer_app.py").run()
    assert not main.exception and main.title[0].value == "서버에 단어장 등록"


def test_ui_partial_failure_retries_only_selected_server(banks, postgres_database_factory, tmp_path, monkeypatch):
    source, local = banks
    production = postgres_database_factory()
    for key, value in (("DATABASE_URL", source), ("QUESTION_BANK_LOCAL_DATABASE_URL", local),
                       ("QUESTION_BANK_PRODUCTION_DATABASE_URL", production)):
        monkeypatch.setenv(key, value)
    original, calls = transfer.execute_vocabulary_transfer, []

    def fail_once(source_url, target_url, plan, root):
        calls.append(target_url)
        if target_url == production and calls.count(production) == 1:
            raise transfer.TransferError("테스트 연결 실패")
        return original(source_url, target_url, plan, root)

    monkeypatch.setattr(transfer, "execute_vocabulary_transfer", fail_once)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.multiselect[0].set_value(["로컬 서버", "운영 서버"]).run()
    app.button[0].click().run()
    assert not app.exception and stored(local) is None and stored(production) is None
    next(b for b in app.button if b.label == "선택 서버에 단어장 등록").click().run()
    assert not app.exception
    assert any("로컬 서버:" in s.value for s in app.success)
    assert any("운영 서버: 테스트 연결 실패" in s.value for s in app.warning)
    assert stored(local) == stored(source) and stored(production) is None
    app.multiselect[0].set_value(["운영 서버"]).run()
    app.button[0].click().run()
    next(b for b in app.button if b.label == "선택 서버에 단어장 등록").click().run()
    assert not app.exception and len(app.success) == 2
    assert calls == [local, production, production]
    assert stored(production) == stored(source)


def test_ui_duplicate_destinations_block_registration(banks, monkeypatch):
    source, target = banks
    for key, value in (("DATABASE_URL", source), ("QUESTION_BANK_LOCAL_DATABASE_URL", target),
                       ("QUESTION_BANK_PRODUCTION_DATABASE_URL", target)):
        monkeypatch.setenv(key, value)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    app.multiselect[0].set_value(["로컬 서버", "운영 서버"]).run()
    app.button[0].click().run()
    assert not app.exception and any("같은 대상 DB" in e.value for e in app.error)
    assert next(b for b in app.button if b.label == "선택 서버에 단어장 등록").disabled
    assert stored(target) is None
