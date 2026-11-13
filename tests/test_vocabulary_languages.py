import json
from collections import Counter
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from test_import_vocabulary import SAMPLE, local_bank, write_csv
from test_vocabulary import bank, PAGE
from test_vocabulary_transfer import banks, prepare, send, stored
from topik_question_lab.maintenance import import_vocabulary as importer
from topik_question_lab.menus.vocabulary_transfer import vocabulary_transfer as transfer
from topik_question_lab.common.vocabulary.vocabulary import search_vocabulary
from topik_question_lab.common.vocabulary.vocabulary_audio import prepare_audio_schema
from topik_question_lab.common.vocabulary.vocabulary_languages import LANGUAGE_FIELDS, LANGUAGE_MIGRATION, apply_language_migration


def add_languages(url):
    with psycopg.connect(url) as c:
        apply_language_migration(c)
        c.execute("UPDATE topik_bank.vocabulary SET chinese_simplified='苹果', indonesian='apel' WHERE vocabulary_id=1")


def test_bundled_csv_has_both_meanings_for_every_word():
    path = Path(__file__).resolve().parents[1] / "TOPIK-II-Word" / "topik-2-all.csv"
    rows = importer.read_vocabulary(path)
    assert len(rows) == 8258
    assert Counter(row[7] for row in rows) == {3: 1581, 4: 2078, 5: 2131, 6: 2468}
    assert all(len(row) == 10 and row[8].strip() and row[9].strip() for row in rows)


def test_extended_import_preserves_ids_and_legacy_import_preserves_translations(local_bank):
    url, root, path, _ = local_bank
    importer.import_vocabulary(root, url, path)
    before = stored(url)
    extended = [(*row, "词语", "kata") for row in SAMPLE]
    write_csv(path, extended, importer.EXTENDED_HEADERS)
    assert importer.read_vocabulary(path)[0] == (*SAMPLE[0][:7], 3, "词语", "kata")
    report = importer.import_vocabulary(root, url, path)
    assert report["updated"] == 3 and report["levels"] == {3: 1, 4: 1, 6: 1}
    after = stored(url)
    assert [{k: v for k, v in row.items() if k not in LANGUAGE_FIELDS} for row in after] == before
    assert importer.import_vocabulary(root, url, path)["unchanged"] == 3
    for rows, headers in [(SAMPLE, importer.HEADERS),
                          ([(*row, "", " ") for row in SAMPLE], importer.EXTENDED_HEADERS)]:
        write_csv(path, rows, headers)
        importer.import_vocabulary(root, url, path)
        assert stored(url) == after


def test_search_and_ui_include_both_languages(bank):
    add_languages(bank)
    for query in ("苹果", "APEL"):
        result = search_vocabulary(bank, query=query)
        assert result["total"] == 1 and result["rows"][0]["korean"] == "사과"
    app = AppTest.from_file(str(PAGE)).run()
    app.text_input(key="vocabulary-query").set_value("苹果").run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["중국어(간체)"] == "苹果"
    assert app.dataframe[0].value.iloc[0]["인도네시아어"] == "apel"
    assert {"苹果", "apel"}.issubset({element.value for element in app.text})


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("audio_first", [False, True])
def test_transfer_upgrade_migration_order_and_backup_restore(banks, tmp_path, postgres_database_factory,
                                                           existing, audio_first):
    source, target = banks
    if existing:
        send(source, target, tmp_path)
        prepare_audio_schema(target, tmp_path)
    if audio_first:
        prepare_audio_schema(source, tmp_path)
    add_languages(source)
    if not audio_first:
        prepare_audio_schema(source, tmp_path)
    plan = prepare(source, target)
    assert plan.upgrade_languages is existing
    send(source, target, tmp_path)
    assert stored(source) == stored(target)
    before = stored(target)
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET indonesian='buah apel' WHERE vocabulary_id=1")
    result = send(source, target, tmp_path)
    backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
    restored = postgres_database_factory()
    with psycopg.connect(restored) as c:
        c.execute("CREATE SCHEMA topik_bank")
        c.execute(backup["create_table_sql"])
        transfer._upsert(c, backup["rows"])
    assert stored(restored) == before
    assert send(source, target, tmp_path)["unchanged"] == 4
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.vocabulary SET chinese_simplified='',indonesian='' WHERE vocabulary_id=1")
    send(source, target, tmp_path)
    assert stored(target) == stored(source)
    assert stored(target)[0]["indonesian"] == stored(target)[0]["chinese_simplified"] == ""


def test_failed_language_upgrade_rolls_back(banks, tmp_path, monkeypatch):
    source, target = banks
    send(source, target, tmp_path)
    before = stored(target)
    add_languages(source)
    monkeypatch.setattr(transfer, "_verify", lambda *args: (_ for _ in ()).throw(RuntimeError("test failure")))
    with pytest.raises(transfer.TransferError):
        send(source, target, tmp_path)
    assert stored(target) == before


def test_import_failure_rolls_back_language_columns(local_bank, monkeypatch):
    url, root, path, _ = local_bank
    importer.import_vocabulary(root, url, path)
    before = stored(url)
    write_csv(path, [(*row, "词语", "kata") for row in SAMPLE], importer.EXTENDED_HEADERS)
    monkeypatch.setattr(importer, "_upsert_vocabulary", lambda *args: (_ for _ in ()).throw(RuntimeError("test failure")))
    with pytest.raises(importer.VocabularyImportError):
        importer.import_vocabulary(root, url, path)
    assert stored(url) == before
    with psycopg.connect(url) as c:
        assert not c.execute("SELECT 1 FROM topik_bank.schema_migrations WHERE version=%s",
                             (LANGUAGE_MIGRATION.stem,)).fetchall()
