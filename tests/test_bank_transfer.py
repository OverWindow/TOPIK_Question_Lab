from copy import deepcopy
from pathlib import Path
import uuid

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.step_06_publication.bank_transfer import (
    TABLES, TransferError, _all_rows, _upsert, capture_source, connection,
    execute_transfer, initialize_target, prepare_transfer,
)
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank
from topik_question_lab.step_06_publication.publication import build_set_draft
from test_unversioned_questions import candidate


@pytest.fixture
def banks(postgres_database_factory):
    source, target = postgres_database_factory(), postgres_database_factory()
    bank = PostgresQuestionBank(source)
    bank.ensure_schema()
    first = bank.publish_set(build_set_draft([candidate(n) for n in range(1, 51)], {}, 4, 0.0))
    second = bank.publish_set(build_set_draft([candidate(n, offset=100) for n in range(1, 51)], {}, 4, 0.0))
    assert initialize_target(source, target)
    return source, target, first.set_id, second.set_id


def send(source, target, ids=None):
    plan = prepare_transfer(capture_source(source, ids), target)
    return execute_transfer(source, target, plan)


def rows(url):
    with connection(url) as c:
        return _all_rows(c)


def add_media(url, set_id):
    with psycopg.connect(url) as c:
        item_id, content_hash = c.execute("SELECT item_id,content_hash FROM topik_bank.items WHERE set_id=%s AND position=1", (set_id,)).fetchone()
        audio = str(uuid.uuid4())
        c.execute("""INSERT INTO topik_bank.tts_audio_assets(audio_asset_id,source_hash,model_name,
            female_voice,male_voice,storage_bucket,storage_path,storage_url,byte_size,storage_backend,file_sha256)
            VALUES (%s,%s,'synthetic','female','male','test-bucket','audio.mp3','https://media.example/audio.mp3',1,'s3',%s)""", (audio, 'a'*64, 'b'*64))
        c.execute("INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash) VALUES (%s,%s,%s,%s)", (item_id, content_hash, audio, 'a'*64))
        c.execute("INSERT INTO topik_bank.question_set_item_audio_bindings(set_id,position,audio_asset_id,source_hash) VALUES (%s,1,%s,%s)", (set_id, audio, 'a'*64))
        visual = str(uuid.uuid4())
        c.execute("""INSERT INTO topik_bank.item_visual_assets(visual_asset_id,item_id,content_hash,option_number,
            storage_bucket,storage_path,storage_url,mime_type,byte_size,storage_backend,file_sha256)
            VALUES (%s,%s,%s,1,'test-bucket','image.png','https://media.example/image.png','image/png',1,'s3',%s)""", (visual, item_id, content_hash, 'c'*64))
    return str(item_id), audio, visual


def test_initialization_copies_only_question_schema_and_is_repeatable(banks):
    source, target, _, _ = banks
    assert initialize_target(source, target) is False
    with connection(target) as c:
        assert {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='topik_bank'")} == set(TABLES)
    with pytest.raises(TransferError, match="원본"):
        initialize_target(source, source)
    with pytest.raises(TransferError, match="원본"):
        prepare_transfer(capture_source(source), source)


def test_all_rows_and_media_are_exact_and_retry_does_not_duplicate(banks):
    source, target, first, _ = banks
    add_media(source, first)
    with psycopg.connect(source) as c:
        c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash,is_current)
            SELECT item_id,%s,audio_asset_id,source_hash,false FROM topik_bank.item_audio_bindings""", ('d'*64,))
        c.execute("""INSERT INTO topik_bank.tts_audio_assets(audio_asset_id,source_hash,model_name,
            female_voice,male_voice,storage_bucket,storage_path,storage_url,byte_size,deleted_at)
            VALUES (%s,%s,'archive','female','male','archive','old.mp3','https://media.example/old.mp3',1,now())""",
                  (uuid.uuid4(), 'e'*64))
    before = rows(source)
    result = send(source, target)
    assert result['counts']['items'] == 100
    assert rows(source) == before == rows(target)
    assert send(source, target)['status'] == 'verified'
    assert rows(target) == before


def test_selected_set_preserves_other_set_and_unassigns_removed_item(banks):
    source, target, first, second = banks
    add_media(source, first)
    send(source, target)
    original = rows(target)
    item_id = next(r['item_id'] for r in original['items'] if r['set_id'] == first and r['position'] == 2)
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE item_id=%s", (item_id,))
        c.execute("UPDATE topik_bank.items SET stem='changed' WHERE set_id=%s AND position=1", (first,))
    assert send(source, target, [first])['detached'] == 1
    after = rows(target)
    assert [r for r in after['items'] if r['set_id'] == second] == [r for r in original['items'] if r['set_id'] == second]
    detached = next(r for r in after['items'] if r['item_id'] == item_id)
    assert detached['set_id'] is None and detached['position'] is None
    assert next(r for r in after['items'] if r['set_id'] == first and r['position'] == 1)['stem'] == 'changed'


def test_position_swap_preserves_ids_and_external_item_references(banks):
    source, target, first, _ = banks
    send(source, target)
    with psycopg.connect(target) as c:
        c.execute("CREATE TABLE public.synthetic_answers(item_id UUID REFERENCES topik_bank.items(item_id))")
        c.execute("INSERT INTO public.synthetic_answers SELECT item_id FROM topik_bank.items WHERE set_id=%s", (first,))
    with psycopg.connect(source) as c:
        values = c.execute("SELECT item_id,position FROM topik_bank.items WHERE set_id=%s AND position IN (1,2)", (first,)).fetchall()
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE item_id=ANY(%s::uuid[])", ([v[0] for v in values],))
        for item_id, position in values:
            c.execute("UPDATE topik_bank.items SET set_id=%s,position=%s WHERE item_id=%s", (first, 3-position, item_id))
    send(source, target, [first])
    assert rows(target) == rows(source)
    with connection(target) as c:
        assert c.execute("SELECT count(*) FROM public.synthetic_answers").fetchone()[0] == 50


def test_unique_collisions_replace_old_ids_in_all_six_tables(banks):
    source, target, first, _ = banks
    add_media(source, first)
    original = capture_source(source, [first])
    conflicting = deepcopy(original.rows)
    old_set, old_audio = str(uuid.uuid4()), str(uuid.uuid4())
    item_map = {r['item_id']: str(uuid.uuid4()) for r in conflicting['items']}
    for table, values in conflicting.items():
        for row in values:
            if 'set_id' in row:
                row['set_id'] = old_set
            if 'item_id' in row:
                row['item_id'] = item_map[row['item_id']]
            if 'audio_asset_id' in row:
                row['audio_asset_id'] = old_audio
            if 'visual_asset_id' in row:
                row['visual_asset_id'] = str(uuid.uuid4())
    with psycopg.connect(target) as c:
        for table in TABLES:
            _upsert(c, table, conflicting[table])
    send(source, target, [first])
    assert rows(target) == original.rows


def test_content_and_unique_value_swaps_are_source_authoritative(banks):
    source, target, first, _ = banks
    send(source, target)
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.items SET stem='target newer',updated_at='2099-01-01' WHERE set_id=%s", (first,))
    assert send(source, target, [first])['status'] == 'verified'
    assert rows(source) == rows(target)


def test_changes_after_preview_require_new_preview(banks):
    source, target, first, _ = banks
    plan = prepare_transfer(capture_source(source, [first]), target)
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.items SET stem='new source' WHERE set_id=%s", (first,))
    with pytest.raises(TransferError, match="원본이 변경"):
        execute_transfer(source, target, plan)
    assert not rows(target)['items']
    send(source, target)
    plan = prepare_transfer(capture_source(source, [first]), target)
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.items SET stem='new target' WHERE set_id=%s", (first,))
    with pytest.raises(TransferError, match="대상 데이터가 변경"):
        execute_transfer(source, target, plan)


def test_failure_mid_transfer_rolls_back_all_tables(banks, monkeypatch):
    import topik_question_lab.step_06_publication.bank_transfer as transfer
    source, target, first, _ = banks
    add_media(source, first)
    before = rows(target)
    original = transfer._upsert
    def fail(c, table, values):
        if table == 'item_audio_bindings':
            c.execute('SELECT 1/0')
        original(c, table, values)
    monkeypatch.setattr(transfer, '_upsert', fail)
    with pytest.raises(TransferError):
        send(source, target)
    assert rows(target) == before


def test_source_item_in_unselected_target_set_is_blocked(banks):
    source, target, first, second = banks
    send(source, target)
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE set_id=%s AND position=1", (second,))
        c.execute("UPDATE topik_bank.items SET set_id=%s WHERE set_id=%s AND position=1", (second, first))
    before = rows(target)
    with pytest.raises(TransferError, match="다른 대상 세트"):
        send(source, target, [first])
    assert rows(target) == before
    send(source, target, [first, second])
    assert rows(target) == rows(source)


def test_foreign_reference_to_replaced_id_blocks_without_cascade(banks):
    source, target, first, _ = banks
    send(source, target)
    with psycopg.connect(target) as c:
        c.execute("CREATE TABLE public.synthetic_answers(item_id UUID REFERENCES topik_bank.items(item_id) ON DELETE CASCADE)")
        old_id = c.execute("SELECT item_id FROM topik_bank.items WHERE set_id=%s AND position=1", (first,)).fetchone()[0]
        c.execute("UPDATE topik_bank.items SET item_id=%s WHERE item_id=%s", (uuid.uuid4(), old_id))
        c.execute("INSERT INTO public.synthetic_answers SELECT item_id FROM topik_bank.items WHERE set_id=%s AND position=1", (first,))
    before = rows(target)
    with pytest.raises(TransferError, match="외부 테이블 참조"):
        send(source, target, [first])
    assert rows(target) == before
    with connection(target) as c:
        assert c.execute("SELECT count(*) FROM public.synthetic_answers").fetchone()[0] == 1


def test_mismatched_schema_is_not_modified(banks):
    source, target, _, _ = banks
    with psycopg.connect(target) as c:
        c.execute("ALTER TABLE topik_bank.items ADD COLUMN unexpected TEXT")
    with pytest.raises(TransferError, match="구조가 다릅니다"):
        initialize_target(source, target)
    with pytest.raises(TransferError, match="대상 DB 구조"):
        prepare_transfer(capture_source(source), target)


def test_replacing_item_id_preserves_reused_visual_id(banks):
    source, target, first, _ = banks
    _, _, visual_id = add_media(source, first)
    send(source, target, [first])
    with psycopg.connect(target) as c:
        old_item = c.execute("SELECT item_id FROM topik_bank.item_visual_assets WHERE visual_asset_id=%s", (visual_id,)).fetchone()[0]
        new_item = uuid.uuid4()
        c.execute("UPDATE topik_bank.items SET source_key='temporary' WHERE item_id=%s", (old_item,))
        original = next(r for r in capture_source(source, [first]).rows['items'] if r['item_id'] == str(old_item))
        c.execute("DELETE FROM topik_bank.question_set_item_audio_bindings WHERE set_id=%s AND position=1", (first,))
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE item_id=%s", (old_item,))
        original['item_id'] = str(new_item)
        _upsert(c, 'items', [original])
        c.execute("UPDATE topik_bank.item_visual_assets SET item_id=%s WHERE visual_asset_id=%s", (new_item, visual_id))
        c.execute("DELETE FROM topik_bank.item_audio_bindings WHERE item_id=%s", (old_item,))
        c.execute("DELETE FROM topik_bank.items WHERE item_id=%s", (old_item,))
    send(source, target, [first])
    assert rows(target) == capture_source(source, [first]).rows


def test_commit_acknowledgement_loss_is_recoverable_without_duplicate(banks, monkeypatch):
    import topik_question_lab.step_06_publication.bank_transfer as transfer
    from contextlib import contextmanager
    source, target, first, _ = banks
    plan = prepare_transfer(capture_source(source, [first]), target)
    real = transfer.connection
    @contextmanager
    def uncertain(url, *, readonly=True):
        with real(url, readonly=readonly) as c:
            yield c
        if not readonly:
            raise TransferError('synthetic lost commit acknowledgement')
    monkeypatch.setattr(transfer, 'connection', uncertain)
    with pytest.raises(TransferError):
        execute_transfer(source, target, plan)
    monkeypatch.setattr(transfer, 'connection', real)
    assert execute_transfer(source, target, plan)['status'] == 'verified'
    assert rows(target) == capture_source(source, [first]).rows


def test_shared_audio_changes_do_not_modify_unselected_sets(banks):
    source, target, first, second = banks
    _, audio, _ = add_media(source, first)
    with psycopg.connect(source) as c:
        c.execute("INSERT INTO topik_bank.question_set_item_audio_bindings(set_id,position,audio_asset_id,source_hash) VALUES (%s,1,%s,%s)", (second, audio, 'a'*64))
    send(source, target)
    with psycopg.connect(source) as c:
        c.execute("UPDATE topik_bank.tts_audio_assets SET duration_ms=100 WHERE audio_asset_id=%s", (audio,))
    before = rows(target)
    with pytest.raises(TransferError, match="선택 범위 밖"):
        send(source, target, [first])
    assert rows(target) == before
    send(source, target, [first, second])
    assert rows(target) == rows(source)


def test_empty_source_and_partial_target_do_not_create_partial_schema(postgres_database_factory):
    source, target = postgres_database_factory(), postgres_database_factory()
    PostgresQuestionBank(source).ensure_schema()
    initialize_target(source, target)
    assert send(source, target)['counts']['items'] == 0
    partial = postgres_database_factory()
    with psycopg.connect(partial) as c:
        c.execute('CREATE SCHEMA topik_bank')
        c.execute('CREATE TABLE topik_bank.items(item_id UUID PRIMARY KEY)')
    with pytest.raises(TransferError, match="기존"):
        initialize_target(source, partial)
    with connection(partial) as c:
        assert c.execute("SELECT count(*) FROM pg_tables WHERE schemaname='topik_bank'").fetchone()[0] == 1


def test_media_check_contacts_only_configured_origin(monkeypatch):
    from topik_question_lab.step_06_publication.bank_transfer import Snapshot, check_shared_media
    import httpx
    contacted = []
    def head(self, url, **kwargs):
        assert kwargs['headers']['Accept-Encoding'] == 'identity'
        contacted.append(url)
        return httpx.Response(200, headers={'content-length': '1', 'content-type': 'image/png'})
    monkeypatch.setattr(httpx.Client, 'head', head)
    data = {t: [] for t in TABLES}
    data['items'] = [{'item_id': 'one', 'content_hash': 'hash'}]
    base = {'item_id': 'one', 'content_hash': 'hash', 'is_current': True, 'byte_size': 1,
            'mime_type': 'image/png', 'storage_backend': 's3', 'storage_path': 'image.png'}
    data['item_visual_assets'] = [base | {'storage_url': 'https://media.example/image.png'},
                                   base | {'storage_url': 'https://other.example/image.png'}]
    report = check_shared_media(Snapshot({}, {}, data, None), 'https://media.example')
    assert report['checked'] == report['failed'] == 1
    assert contacted == ['https://media.example/image.png']


def test_ui_preview_and_registration_use_only_explicit_target(banks, monkeypatch):
    source, target, first, _ = banks
    monkeypatch.setenv('DATABASE_URL', source)
    monkeypatch.setenv('QUESTION_BANK_LOCAL_DATABASE_URL', target)
    # A production value must not be contacted when only local is selected.
    monkeypatch.setenv('QUESTION_BANK_PRODUCTION_DATABASE_URL', 'postgresql://invalid.example/unused')
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'topik_question_lab/ui/bank_transfer_app.py'), default_timeout=30).run()
    next(b for b in app.button if b.label == '원본 세트 불러오기').click().run()
    app.multiselect[1].set_value([first]).run()
    next(b for b in app.button if b.label == '등록 내용 확인').click().run()
    assert not app.exception
    assert not rows(target)['items']
    next(b for b in app.button if b.label == '선택 서버에 원본으로 덮어쓰기').click().run()
    assert not app.exception
    assert len(rows(target)['items']) == 50
    app.run()
    assert len(rows(target)['items']) == 50


def test_ui_starts_without_connections_and_defaults_to_local():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'topik_question_lab/ui/bank_transfer_app.py')).run()
    assert not app.exception
    assert app.multiselect[0].value == ['로컬 서버']
    assert next(b for b in app.button if b.label == '등록 내용 확인').disabled
