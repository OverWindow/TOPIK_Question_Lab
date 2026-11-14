from copy import deepcopy
import json
import uuid

import psycopg
import pytest

from test_bank_transfer import banks, add_media, send, rows
from topik_question_lab.common.question_bank.connection import connection
from topik_question_lab.common.question_bank.set_deletion import SOURCE, SetDeletion, affected_rows
from topik_question_lab.menus.bank_transfer.bank_transfer import TABLES, KEYS, _upsert


class FakeDeleter:
    def __init__(self):
        self.calls = []
        self.fail = False

    def validate(self, asset):
        pass

    def preflight(self, assets):
        pass

    def delete(self, asset, token):
        self.calls.append((deepcopy(asset), token))
        if self.fail:
            raise RuntimeError('synthetic failure')
        return True

    def close(self):
        pass


@pytest.fixture
def deletion(banks, tmp_path):
    source, target, first, second = banks
    add_media(source, first)
    send(source, target)
    deleter = FakeDeleter()
    service = SetDeletion(tmp_path, {SOURCE: source, '로컬 서버': target}, deleter=deleter)
    return service, deleter, source, target, first, second


def test_full_set_deletion_preserves_other_set_and_records_no_secrets(deletion):
    service, files, source, target, first, second = deletion
    before = rows(source)
    plan = service.prepare(first, ['로컬 서버'])
    assert rows(source) == rows(target) == before and not service.directory.exists()
    assert all(f['state'] == 'pending' for f in plan['files'])
    result = service.execute(plan)
    assert result['status'] == 'complete' and len(files.calls) == 2
    deleted, _ = affected_rows(before, first)
    expected = {table: [r for r in before[table] if r not in deleted[table]] for table in TABLES}
    assert rows(source) == rows(target) == expected
    assert expected['question_sets'][0]['set_id'] == second
    journal = service._path(plan['id']).read_text(encoding='utf-8')
    assert source not in journal and target not in journal and 'content_json' not in journal
    with pytest.raises(ValueError, match='이미 시작'):
        service.execute(plan)
    assert service.resume(plan['id'])['status'] == 'complete' and len(files.calls) == 2


def test_unselected_server_preserves_all_its_data_and_files(deletion):
    service, files, source, target, first, _ = deletion
    before = rows(target)
    plan = service.prepare(first, [])
    assert all(f['state'] == 'preserved' for f in plan['files'])
    assert all('asset' not in f for f in plan['files'])
    assert service.execute(plan)['status'] == 'complete'
    assert rows(target) == before and not files.calls
    assert not any(r['set_id'] == first for r in rows(source)['question_sets'])


def test_other_set_keeps_shared_audio_and_same_file_image(deletion):
    service, files, source, target, first, second = deletion
    original = rows(source)
    item = next(r for r in original['items'] if r['set_id'] == second and r['position'] == 1)
    audio = original['tts_audio_assets'][0]
    image = {**original['item_visual_assets'][0], 'visual_asset_id': str(uuid.uuid4()), 'item_id': item['item_id']}
    with psycopg.connect(source) as c:
        c.execute('INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash) VALUES (%s,%s,%s,%s)',
                  (item['item_id'], item['content_hash'], audio['audio_asset_id'], audio['source_hash']))
        _upsert(c, 'item_visual_assets', [image])
    plan = service.prepare(first, ['로컬 서버'])
    assert service.execute(plan)['status'] == 'complete' and not files.calls
    remaining = rows(source)
    assert remaining['tts_audio_assets'] == [audio] and remaining['item_visual_assets'] == [image]
    assert len(remaining['item_audio_bindings']) == 1


def test_reference_using_same_url_with_different_metadata_is_preserved(deletion):
    service, files, _, target, first, _ = deletion
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.item_visual_assets SET storage_path='alias.png'")
    plan = service.prepare(first, [])
    assert all(f['state'] == 'preserved' for f in plan['files'])
    service.execute(plan)
    assert not files.calls


def test_historical_media_are_included_in_explicit_deletion(deletion):
    service, files, source, target, first, _ = deletion
    original = rows(source)
    audio = {**original['tts_audio_assets'][0], 'audio_asset_id': str(uuid.uuid4()), 'source_hash': 'e' * 64,
             'storage_path': 'old.mp3', 'storage_url': 'https://media.example/old.mp3'}
    binding = {**original['item_audio_bindings'][0], 'binding_id': str(uuid.uuid4()),
               'audio_asset_id': audio['audio_asset_id'], 'is_current': False}
    image = {**original['item_visual_assets'][0], 'visual_asset_id': str(uuid.uuid4()), 'is_current': False,
             'storage_path': 'old.png', 'storage_url': 'https://media.example/old.png'}
    with psycopg.connect(source) as c:
        for table, row in [('tts_audio_assets', audio), ('item_audio_bindings', binding), ('item_visual_assets', image)]:
            _upsert(c, table, [row])
    plan = service.prepare(first, ['로컬 서버'])
    assert len(plan['files']) == 4
    assert service.execute(plan)['status'] == 'complete' and len(files.calls) == 4
    assert not rows(source)['tts_audio_assets']


@pytest.mark.parametrize('table', ['question_sets', 'items', 'item_visual_assets', 'tts_audio_assets'])
def test_external_cascade_reference_blocks_every_database(deletion, table):
    service, files, source, target, first, _ = deletion
    record = rows(target)[table][0]
    if table == 'question_sets':
        record = next(r for r in rows(target)[table] if r['set_id'] == first)
    elif table == 'items':
        record = next(r for r in rows(target)[table] if r['set_id'] == first)
    key = KEYS[table][0]
    with psycopg.connect(target) as c:
        c.execute(f'CREATE TABLE public.synthetic_usage(ref uuid REFERENCES topik_bank.{table}({key}) ON DELETE CASCADE)')
        c.execute('INSERT INTO public.synthetic_usage VALUES (%s)', (record[key],))
    before = rows(source), rows(target)
    with pytest.raises(ValueError, match='사용 기록'):
        service.prepare(first, ['로컬 서버'])
    assert (rows(source), rows(target)) == before and not files.calls
    with connection(target) as c:
        assert c.execute('SELECT count(*) FROM public.synthetic_usage').fetchone()[0] == 1


def test_changed_selected_set_aborts_before_any_delete(deletion):
    service, files, source, target, first, _ = deletion
    plan = service.prepare(first, ['로컬 서버'])
    with psycopg.connect(target) as c:
        c.execute("UPDATE topik_bank.items SET stem='concurrent edit' WHERE set_id=%s", (first,))
    before = rows(source), rows(target)
    with pytest.raises(ValueError, match='변경'):
        service.execute(plan)
    assert (rows(source), rows(target)) == before and not files.calls


def test_unavailable_observer_blocks_preview_and_execution(deletion, monkeypatch):
    import topik_question_lab.common.question_bank.set_deletion as module
    service, files, source, target, first, _ = deletion
    plan = service.prepare(first, [])
    original = module.connection
    def offline(url, **kwargs):
        if url == target:
            raise ValueError('synthetic offline server')
        return original(url, **kwargs)
    monkeypatch.setattr(module, 'connection', offline)
    before = rows(source), rows(target)
    with pytest.raises(ValueError, match='offline'):
        service.prepare(first, [])
    with pytest.raises(ValueError, match='offline'):
        service.execute(plan)
    assert (rows(source), rows(target)) == before and not files.calls


def test_partial_database_failure_resumes_without_redeleting_committed_database(deletion, monkeypatch):
    service, files, source, target, first, _ = deletion
    plan = service.prepare(first, ['로컬 서버'])
    original, calls = service._delete_rows, []
    def fail_second(c, deleted):
        calls.append(c)
        if len(calls) == 2:
            raise RuntimeError('synthetic failure')
        original(c, deleted)
    monkeypatch.setattr(service, '_delete_rows', fail_second)
    partial = service.execute(plan)
    assert partial['status'] == 'pending' and not files.calls
    assert sorted(d['state'] for d in partial['databases'].values()) == ['done', 'started']
    monkeypatch.setattr(service, '_delete_rows', original)
    restarted = SetDeletion(service.root, service.banks, deleter=files)
    assert restarted.resume(plan['id'])['status'] == 'complete' and len(files.calls) == 2
    assert not any(r['set_id'] == first for r in rows(source)['question_sets'] + rows(target)['question_sets'])


def test_file_retry_preserves_new_reference_after_database_deletion(deletion):
    service, files, source, target, first, _ = deletion
    asset = rows(source)['tts_audio_assets'][0]
    files.fail = True
    plan = service.prepare(first, ['로컬 서버'])
    assert service.execute(plan)['status'] == 'pending'
    with psycopg.connect(target) as c:
        _upsert(c, 'tts_audio_assets', [asset])
    files.calls.clear()
    files.fail = False
    result = service.resume(plan['id'])
    assert result['status'] == 'complete'
    assert [r['state'] for r in result['files']].count('preserved') == 1
    assert len(files.calls) == 1 and files.calls[0][0]['storage_path'] == 'image.png'
    assert rows(target)['tts_audio_assets'] == [asset]


def test_reference_created_during_commit_is_preserved(deletion, monkeypatch):
    service, files, source, target, first, _ = deletion
    asset = rows(source)['tts_audio_assets'][0]
    original = service._delete_rows
    def delete_and_add_reference(c, deleted):
        original(c, deleted)
        _upsert(c, 'tts_audio_assets', [asset])
    monkeypatch.setattr(service, '_delete_rows', delete_and_add_reference)
    result = service.execute(service.prepare(first, ['로컬 서버']))
    assert result['status'] == 'complete'
    assert len(files.calls) == 1 and files.calls[0][0]['storage_path'] == 'image.png'


def test_reconfigured_bank_and_reregistered_set_block_retry(deletion):
    service, files, source, target, first, _ = deletion
    backup = rows(source)
    files.fail = True
    plan = service.prepare(first, ['로컬 서버'])
    service.execute(plan)
    changed = SetDeletion(service.root, {SOURCE: source}, deleter=files)
    assert len(changed.pending()) == 1
    with pytest.raises(ValueError, match='설정'):
        changed.resume(plan['id'])
    with psycopg.connect(target) as c:
        _upsert(c, 'question_sets', [r for r in backup['question_sets'] if r['set_id'] == first])
    files.calls.clear()
    assert '다시 등록' in service.resume(plan['id'])['error']
    assert not files.calls


def test_empty_set_can_be_deleted_without_files(banks, tmp_path):
    source, target, first, _ = banks
    with psycopg.connect(source) as c:
        c.execute('UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE set_id=%s', (first,))
    files = FakeDeleter()
    service = SetDeletion(tmp_path, {SOURCE: source, '로컬 서버': target}, deleter=files)
    plan = service.prepare(first, [])
    assert plan['databases'][SOURCE]['counts']['items'] == 0
    assert service.execute(plan)['status'] == 'complete' and not files.calls
    assert len(rows(source)['items']) == 100


def test_lost_commit_acknowledgement_recovers_without_repeating_delete(deletion, monkeypatch):
    from contextlib import contextmanager
    import topik_question_lab.common.question_bank.set_deletion as module
    service, files, source, target, first, _ = deletion
    plan = service.prepare(first, ['로컬 서버'])
    original = module.connection
    lost = []

    class CommitLoss:
        def __init__(self, c):
            self.c = c
        def __getattr__(self, name):
            return getattr(self.c, name)
        def commit(self):
            self.c.commit()
            if not lost:
                lost.append(True)
                raise RuntimeError('acknowledgement lost')

    @contextmanager
    def connection_loss(url, **kwargs):
        with original(url, **kwargs) as c:
            yield CommitLoss(c)

    monkeypatch.setattr(module, 'connection', connection_loss)
    partial = service.execute(plan)
    assert partial['status'] == 'pending' and not files.calls
    monkeypatch.setattr(module, 'connection', original)
    assert service.resume(plan['id'])['status'] == 'complete' and len(files.calls) == 2
    assert not any(r['set_id'] == first for r in rows(source)['question_sets'] + rows(target)['question_sets'])


def test_provider_preflight_failure_never_deletes_database_rows(deletion, monkeypatch):
    service, files, source, target, first, _ = deletion
    plan = service.prepare(first, ['로컬 서버'])
    before = rows(source), rows(target)
    def fail(assets):
        raise ValueError('synthetic storage configuration failure')
    monkeypatch.setattr(files, 'preflight', fail)
    with pytest.raises(ValueError, match='storage configuration'):
        service.execute(plan)
    assert (rows(source), rows(target)) == before and not files.calls
    assert not service._path(plan['id']).exists()
