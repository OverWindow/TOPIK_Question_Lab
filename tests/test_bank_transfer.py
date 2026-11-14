from copy import deepcopy
from pathlib import Path
import json
import uuid

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.menus.bank_transfer.bank_transfer import TABLES, _all_rows, _upsert, capture_source, execute_transfer, initialize_target, prepare_transfer, schema_description
from topik_question_lab.common.question_bank.connection import TransferError, connection, digest
from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank
from topik_question_lab.common.question_bank.publication import build_set_draft
from test_unversioned_questions import candidate


@pytest.fixture(autouse=True)
def data_editor_widget_events(monkeypatch):
    from streamlit.proto.WidgetStates_pb2 import WidgetState
    import streamlit.testing.v1.element_tree as elements

    original = elements.get_widget_state

    def widget_state(node):
        # AppTest omits data editors; browsers resend their current edits on clicks.
        if isinstance(node, elements.Dataframe) and node.proto.id and node.proto.editing_mode:
            return WidgetState(id=node.proto.id, string_value=json.dumps(node.root.session_state[node.proto.id]))
        return original(node)

    monkeypatch.setattr(elements, 'get_widget_state', widget_state)


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
            female_voice,male_voice,storage_bucket,storage_path,storage_url,byte_size,storage_backend,file_sha256,
            narration_version,script_snapshot)
            VALUES (%s,%s,'synthetic','female','male','test-bucket','audio.mp3','https://media.example/audio.mp3',1,'s3',%s,
                    'exam_track_v4','{}')""", (audio, 'a'*64, 'b'*64))
        c.execute("INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash) VALUES (%s,%s,%s,%s)", (item_id, content_hash, audio, 'a'*64))
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
            female_voice,male_voice,storage_bucket,storage_path,storage_url,byte_size,deleted_at,script_snapshot)
            VALUES (%s,%s,'archive','female','male','archive','old.mp3','https://media.example/old.mp3',1,now(),'{}')""",
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


def test_unique_collisions_replace_old_ids_in_all_five_tables(banks):
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
    import topik_question_lab.menus.bank_transfer.bank_transfer as transfer
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


@pytest.fixture
def legacy_audio_banks(banks):
    _, target, _, _ = banks
    with psycopg.connect(target) as c:
        c.execute("""ALTER TABLE topik_bank.tts_audio_assets
            ALTER COLUMN script_snapshot DROP NOT NULL,
            ALTER COLUMN narration_version SET DEFAULT 'dialogue_v1',
            DROP CONSTRAINT tts_audio_assets_narration_version_check,
            ADD CONSTRAINT tts_audio_assets_narration_version_check
                CHECK (narration_version IN ('dialogue_v1','exam_track_v2','exam_track_v3','exam_track_v4')),
            ADD CONSTRAINT tts_audio_assets_check
                CHECK (narration_version='dialogue_v1' OR script_snapshot IS NOT NULL)""")
    return banks


def test_v4_transfer_preserves_legacy_audio_and_unselected_set(legacy_audio_banks):
    source, target, first, second = legacy_audio_banks
    add_media(source, first)
    send(source, target, [second])
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.items SET stem='existing server content' WHERE set_id=%s", (second,))
        item_id, content_hash = c.execute(
            "SELECT item_id,content_hash FROM topik_bank.items WHERE set_id=%s AND position=1", (second,)).fetchone()
        for version in ('dialogue_v1', 'exam_track_v2', 'exam_track_v3', 'exam_track_v4'):
            audio, source_hash = uuid.uuid4(), uuid.uuid4().hex * 2
            c.execute("""INSERT INTO topik_bank.tts_audio_assets(audio_asset_id,source_hash,model_name,
                female_voice,male_voice,storage_bucket,storage_path,storage_url,byte_size,
                narration_version,script_snapshot)
                VALUES (%s,%s,'legacy','female','male','test','legacy.mp3',
                    'https://media.example/legacy.mp3',1,%s,%s::jsonb)""",
                      (audio, source_hash, version, None if version == 'dialogue_v1' else '{}'))
            c.execute("""INSERT INTO topik_bank.item_audio_bindings
                (item_id,content_hash,audio_asset_id,source_hash,is_current) VALUES (%s,%s,%s,%s,%s)""",
                      (item_id, content_hash, audio, source_hash, version == 'exam_track_v4'))
    original_source, before = rows(source), rows(target)
    with connection(target) as c:
        original_schema = schema_description(c)
    assert initialize_target(source, target) is False
    snapshot = capture_source(source, [first])
    plan = prepare_transfer(snapshot, target)
    assert plan.target_signature == digest((original_schema, before))
    assert rows(target) == before
    assert execute_transfer(source, target, plan)['status'] == 'verified'
    after = rows(target)
    for table in TABLES:
        expected = before[table] + snapshot.rows[table]
        assert len(after[table]) == len(expected)
        assert all(row in after[table] for row in expected)
    assert send(source, target, [first])['status'] == 'verified'
    assert rows(target) == after
    assert rows(source) == original_source
    with connection(target) as c:
        assert schema_description(c) == original_schema


@pytest.mark.parametrize('ddl,problem', [
    ("""ALTER TABLE topik_bank.tts_audio_assets
        DROP CONSTRAINT tts_audio_assets_narration_version_check,
        ADD CONSTRAINT tts_audio_assets_narration_version_check
            CHECK (narration_version IN ('dialogue_v1','exam_track_v2','exam_track_v3'))""", 'CHECK'),
    ('ALTER TABLE topik_bank.tts_audio_assets DROP CONSTRAINT tts_audio_assets_check', 'CHECK'),
    ('ALTER TABLE topik_bank.tts_audio_assets ALTER COLUMN script_snapshot TYPE TEXT USING script_snapshot::text', '컬럼'),
    ('ALTER TABLE topik_bank.items ALTER COLUMN stem DROP NOT NULL', '컬럼'),
    ('ALTER TABLE topik_bank.item_audio_bindings DROP CONSTRAINT item_audio_bindings_audio_asset_id_fkey', '외래 키'),
    ('ALTER TABLE topik_bank.question_sets DROP CONSTRAINT question_sets_exam_sequence_key', '고유키'),
    ('DROP INDEX topik_bank.item_audio_current_idx', '인덱스'),
    ("ALTER TABLE topik_bank.tts_audio_assets ADD CONSTRAINT restricted_model CHECK (model_name='restricted')", 'CHECK'),
], ids=['missing-v4', 'missing-script-check', 'script-type', 'other-nullability',
        'missing-fk', 'missing-unique', 'missing-index', 'extra-check'])
def test_legacy_audio_compatibility_still_blocks_other_schema_changes(legacy_audio_banks, ddl, problem):
    source, target, _, _ = legacy_audio_banks
    with psycopg.connect(target) as c:
        c.execute(ddl)
    before = rows(target)
    with connection(target) as c:
        original_schema = schema_description(c)
    with pytest.raises(TransferError, match=problem):
        initialize_target(source, target)
    with pytest.raises(TransferError, match=problem):
        prepare_transfer(capture_source(source), target)
    assert rows(target) == before
    with connection(target) as c:
        assert schema_description(c) == original_schema


def test_legacy_audio_source_cannot_transfer_to_v4_only_target(legacy_audio_banks):
    modern, legacy, _, _ = legacy_audio_banks
    before = rows(modern)
    with pytest.raises(TransferError, match='대상 DB 구조'):
        prepare_transfer(capture_source(legacy), modern)
    assert rows(modern) == before


def test_replacing_item_id_preserves_reused_visual_id(banks):
    source, target, first, _ = banks
    _, _, visual_id = add_media(source, first)
    send(source, target, [first])
    with psycopg.connect(target) as c:
        old_item = c.execute("SELECT item_id FROM topik_bank.item_visual_assets WHERE visual_asset_id=%s", (visual_id,)).fetchone()[0]
        new_item = uuid.uuid4()
        c.execute("UPDATE topik_bank.items SET source_key='temporary' WHERE item_id=%s", (old_item,))
        original = next(r for r in capture_source(source, [first]).rows['items'] if r['item_id'] == str(old_item))
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE item_id=%s", (old_item,))
        original['item_id'] = str(new_item)
        _upsert(c, 'items', [original])
        c.execute("UPDATE topik_bank.item_visual_assets SET item_id=%s WHERE visual_asset_id=%s", (new_item, visual_id))
        c.execute("DELETE FROM topik_bank.item_audio_bindings WHERE item_id=%s", (old_item,))
        c.execute("DELETE FROM topik_bank.items WHERE item_id=%s", (old_item,))
    send(source, target, [first])
    assert rows(target) == capture_source(source, [first]).rows


def test_commit_acknowledgement_loss_is_recoverable_without_duplicate(banks, monkeypatch):
    import topik_question_lab.menus.bank_transfer.bank_transfer as transfer
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
        c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
            SELECT item_id,content_hash,%s,%s FROM topik_bank.items WHERE set_id=%s AND position=1""", (audio, 'a'*64, second))
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
    from topik_question_lab.menus.bank_transfer.bank_transfer import Snapshot, check_shared_media
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
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'topik_question_lab/menus/bank_transfer/bank_transfer_app.py'), default_timeout=30).run()
    assert app.dataframe[0].value['형식'].tolist() == ['PBT', 'PBT']
    select_table_sets(app, [first])
    next(b for b in app.button if b.label == '등록 내용 확인').click().run()
    assert not app.exception
    assert not rows(target)['items']
    next(b for b in app.button if b.label == '선택 서버에 원본으로 덮어쓰기').click().run()
    assert not app.exception
    assert len(rows(target)['items']) == 50
    app.run()
    assert len(rows(target)['items']) == 50


def test_ui_starts_without_connections_and_defaults_to_local():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'topik_question_lab/menus/bank_transfer/bank_transfer_app.py')).run()
    assert not app.exception
    assert app.multiselect[0].value == ['로컬 서버']
    assert next(b for b in app.button if b.label == '등록 내용 확인').disabled


def select_table_sets(app, set_ids):
    # AppTest has no data-editor API; send the same cell edits as the browser.
    table = next(frame for frame in app.dataframe if '_set_id' in frame.value.columns)
    states = app._tree.get_widget_states()
    edit = next((state for state in states.widgets if state.id == table.proto.id), None)
    if edit is None:
        edit = states.widgets.add(id=table.proto.id)
    edit.string_value = json.dumps({'edited_rows': {
        str(index): {'선택': sid in set_ids} for index, sid in enumerate(table.value['_set_id'])
    }, 'added_rows': [], 'deleted_rows': []})
    app._run(states)
    assert not app.exception
    assert set(app.session_state['bank-transfer-selected']) == set(set_ids)
    return app


def test_linked_set_replaces_previous_scope_and_waits_for_explicit_transfer(monkeypatch):
    from types import SimpleNamespace
    import topik_question_lab.menus.bank_transfer.bank_transfer as transfer

    monkeypatch.setenv('DATABASE_URL', 'synthetic-source')
    monkeypatch.setenv('QUESTION_BANK_LOCAL_DATABASE_URL', 'synthetic-target')
    catalog = [dict(set_id=sid, section='reading', model='test', sequence=n, status='reviewed', items=50)
               for n, sid in enumerate(['first', 'second'], 1)]
    reads, captures, writes = [], [], []
    monkeypatch.setattr(transfer, 'list_source_sets', lambda url: reads.append(url) or deepcopy(catalog))
    snapshot = SimpleNamespace(counts={'items': 50})
    plan = SimpleNamespace(snapshot=snapshot, effects={'detached': [], 'replace': {}, 'blockers': []}, already_matches=False)
    monkeypatch.setattr(transfer, 'capture_source', lambda source, ids: captures.append((source, ids)) or snapshot)
    monkeypatch.setattr(transfer, 'check_shared_media', lambda *args:
                        dict(checked=1, failed=0, unverified=0, legacy_or_local=0))
    monkeypatch.setattr(transfer, 'prepare_transfer', lambda value, target: plan)
    monkeypatch.setattr(transfer, 'execute_transfer', lambda source, target, value:
                        writes.append((source, target, value)) or dict(status='verified', counts={'items': 50, 'question_sets': 1}))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'topik_question_lab/menus/bank_transfer/bank_transfer_app.py')).run()
    assert not app.exception and reads == ['synthetic-source']
    app.session_state['bank-transfer-mode'] = '전체 문제은행'
    select_table_sets(app, ['first'])
    next(b for b in app.button if b.label == '등록 내용 확인').click().run()
    assert not app.exception and not writes
    assert captures == [('synthetic-source', ['first'])]
    app.session_state['bank-transfer-set-link'] = 'second'
    app.run()
    assert not app.exception
    assert not app.radio
    assert app.session_state['bank-transfer-selected'] == ['second']
    assert not any(b.label == '선택 서버에 원본으로 덮어쓰기' for b in app.button)
    assert not writes and len(reads) == 2
    next(b for b in app.button if b.label == '등록 내용 확인').click().run()
    assert captures[-1] == ('synthetic-source', ['second']) and not writes
    next(b for b in app.button if b.label == '선택 서버에 원본으로 덮어쓰기').click().run()
    assert not app.exception and writes == [('synthetic-source', 'synthetic-target', plan)]
    app.run()
    assert not app.exception and len(writes) == 1 and len(reads) == 2
    assert app.session_state['bank-transfer-selected'] == ['second']


def test_missing_linked_set_clears_old_selection_and_refresh_prunes_removed_sets(monkeypatch):
    import topik_question_lab.menus.bank_transfer.bank_transfer as transfer

    monkeypatch.setenv('DATABASE_URL', 'synthetic-source')
    monkeypatch.setenv('QUESTION_BANK_LOCAL_DATABASE_URL', 'synthetic-target')
    catalog = [dict(set_id='first', section='reading', model='test', sequence=1, status='reviewed', items=50)]
    monkeypatch.setattr(transfer, 'list_source_sets', lambda url: deepcopy(catalog))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'topik_question_lab/menus/bank_transfer/bank_transfer_app.py')).run()
    select_table_sets(app, ['first'])
    app.session_state['bank-transfer-set-link'] = 'missing'
    app.run()
    assert not app.exception and app.session_state['bank-transfer-selected'] == []
    assert any('선택한 세트를 불러오지 못했습니다' in warning.value for warning in app.warning)
    assert next(b for b in app.button if b.label == '등록 내용 확인').disabled
    select_table_sets(app, ['first'])
    catalog.clear()
    next(b for b in app.button if b.label == '세트 목록 새로고침').click().run()
    assert not app.exception and app.session_state['bank-transfer-selected'] == []
    assert next(b for b in app.button if b.label == '등록 내용 확인').disabled


@pytest.fixture
def dated_selection_page(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from topik_question_lab.common.question_bank.direct_publication import batch_set_id
    import topik_question_lab.menus.bank_transfer.bank_transfer as transfer

    db = tmp_path / 'data/bulk_generation.db'
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE batches(id TEXT PRIMARY KEY, section TEXT, created_at TEXT)')
        connection.executemany('INSERT INTO batches VALUES (?, ?, ?)', [
            ('first', 'reading', '2026-11-13 23:00:00'), ('second', 'listening', '2026-11-14 00:30:00')])
    ids = [batch_set_id('reading', 'first'), batch_set_id('listening', 'second'), str(uuid.uuid4())]
    catalog = [dict(set_id=sid, section='reading' if index != 1 else 'listening', model=f'model-{index}',
                    sequence=index + 1, status='reviewed', items=30 if index == 1 else 50,
                    exam_format='ibt' if index == 1 else 'pbt', published_at='2026-11-15T00:00:00Z')
               for index, sid in enumerate(ids)]
    captures, writes = [], []
    monkeypatch.setenv('DATABASE_URL', 'synthetic-source')
    monkeypatch.setenv('QUESTION_BANK_LOCAL_DATABASE_URL', 'synthetic-target')
    monkeypatch.setattr(transfer, 'list_source_sets', lambda url: deepcopy(catalog))

    def capture(source, selected):
        captures.append(list(selected))
        return SimpleNamespace(set_ids=list(selected), counts={
            'items': sum(row['items'] for row in catalog if row['set_id'] in selected), 'question_sets': len(selected)})

    monkeypatch.setattr(transfer, 'capture_source', capture)
    monkeypatch.setattr(transfer, 'check_shared_media', lambda *args: dict(checked=0, failed=0, unverified=0, legacy_or_local=0))
    monkeypatch.setattr(transfer, 'prepare_transfer', lambda value, target: SimpleNamespace(
        snapshot=value, effects={'detached': [], 'replace': {}, 'blockers': []}, already_matches=False))
    monkeypatch.setattr(transfer, 'execute_transfer', lambda source, target, plan:
                        writes.append(list(plan.snapshot.set_ids)) or dict(status='verified', counts=plan.snapshot.counts))
    before = db.read_bytes()
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'topik_question_lab/menus/bank_transfer/bank_transfer_app.py')).run()
    assert not app.exception and db.read_bytes() == before
    return SimpleNamespace(app=app, catalog=catalog, ids=ids, db=db, captures=captures, writes=writes)


def test_selection_table_shows_actual_generation_dates_in_kst(dated_selection_page):
    page = dated_selection_page
    table = page.app.dataframe[0].value
    assert table['_set_id'].tolist() == [page.ids[1], page.ids[0], page.ids[2]]
    assert table['생성일시(KST)'].tolist() == ['2026/11/14 09:30', '2026/11/14 08:00', '기록 없음']
    assert table['형식'].tolist() == ['IBT', 'PBT', 'PBT']
    assert table['영역'].tolist() == ['듣기', '읽기', '읽기']
    assert not table['선택'].any()
    assert any(c.value == '선택 0개 세트 · 총 0문항' for c in page.app.caption)
    assert next(b for b in page.app.button if b.label == '등록 내용 확인').disabled


def test_only_currently_checked_sets_can_be_transferred(dated_selection_page):
    page, app = dated_selection_page, dated_selection_page.app
    first, second, _ = page.ids
    select_table_sets(app, [first, second])
    assert any(c.value == '선택 2개 세트 · 총 80문항' for c in app.caption)
    next(b for b in app.button if b.label == '등록 내용 확인').click().run()
    assert set(page.captures[-1]) == {first, second} and not page.writes
    select_table_sets(app, [])
    assert not any(b.label == '선택 서버에 원본으로 덮어쓰기' for b in app.button)
    assert next(b for b in app.button if b.label == '등록 내용 확인').disabled
    select_table_sets(app, [first])
    assert not any(b.label == '선택 서버에 원본으로 덮어쓰기' for b in app.button)
    next(b for b in app.button if b.label == '등록 내용 확인').click().run()
    assert page.captures[-1] == [first] and not page.writes
    next(b for b in app.button if b.label == '선택 서버에 원본으로 덮어쓰기').click().run()
    assert not app.exception and page.writes == [[first]]
    app.run()
    assert app.session_state['bank-transfer-selected'] == [first] and page.writes == [[first]]


def test_refresh_reorders_rows_without_moving_checkmarks(dated_selection_page):
    import sqlite3
    from topik_question_lab.common.question_bank.direct_publication import batch_set_id

    page, app = dated_selection_page, dated_selection_page.app
    first = page.ids[0]
    select_table_sets(app, [first])
    newest = batch_set_id('reading', 'newest')
    with sqlite3.connect(page.db) as connection:
        connection.execute('INSERT INTO batches VALUES (?, ?, ?)', ('newest', 'reading', '2026-11-14 01:00:00'))
    page.catalog.append(dict(page.catalog[0], set_id=newest, sequence=4))
    page.catalog.reverse()
    next(b for b in app.button if b.label == '세트 목록 새로고침').click().run()
    assert not app.exception
    table = app.dataframe[0].value
    assert table['_set_id'].tolist() == [newest, page.ids[1], first, page.ids[2]]
    assert table.loc[table['선택'], '_set_id'].tolist() == [first]
    assert app.session_state['bank-transfer-selected'] == [first]
    select_table_sets(app, [first, newest])
    page.catalog[:] = [row for row in page.catalog if row['set_id'] != first]
    next(b for b in app.button if b.label == '세트 목록 새로고침').click().run()
    assert not app.exception and app.session_state['bank-transfer-selected'] == [newest]
    next(b for b in app.button if b.label == '등록 내용 확인').click().run()
    assert page.captures == [[newest]] and not page.writes
