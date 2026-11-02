import json
from pathlib import Path
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from test_direct_publication import seed_batch, change_question
from test_media_generation import seed_media, pipeline
from test_storage_overview import files
from topik_question_lab.common.local_cleanup import cleanup_receipts, batch_was_cleaned
from topik_question_lab.maintenance.backup_service import inspect_backup, restore_backup
from topik_question_lab.step_03_generation.bulk_generation import BulkRunner
from topik_question_lab.step_06_publication.local_set_cleanup import LocalSetCleanup
from topik_question_lab.step_06_publication.media_registration import MediaRegistration
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog
from topik_question_lab.step_06_publication.registration_workflow import RegistrationWorkflow
from topik_question_lab.step_06_publication.storage_overview import load_overview


PAGE = Path(__file__).resolve().parents[1] / "topik_question_lab/ui/postgres_publish_app.py"


@pytest.fixture
def cleanup_bank(tmp_path, postgres_database_factory, monkeypatch):
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    bid = seed_media(tmp_path)
    pipeline(tmp_path, bid, registration=MediaRegistration(tmp_path, url)).run()
    for path in (tmp_path / "data/types").glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
            c.execute("UPDATE reviews SET data_json=?", (json.dumps({"approved": True}),))
    with sqlite3.connect(tmp_path / "data/bulk_generation.db") as c:
        c.execute("UPDATE batches SET pipeline_status='succeeded',auto_resume=0")
    service = LocalSetCleanup(tmp_path, url)
    with service.reader.connect() as c:
        c.execute("UPDATE topik_bank.items SET review_status='reviewed'")
        c.execute("UPDATE topik_bank.question_sets SET review_status='reviewed'")
    monkeypatch.setenv("DATABASE_URL", url)
    return service, bid


def db_files(root):
    return {k: v for k, v in files(root).items() if k.endswith('.db')}


def pg_rows(service):
    with service.reader.connect(True) as c:
        tables = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='topik_bank'")]
        return {t: c.execute(f'SELECT to_jsonb(r) FROM topik_bank."{t}" r ORDER BY 1::text').fetchall() for t in tables}


def plan(service):
    row = service.candidates()[0]
    assert row["count"] == 50 and not row["reasons"]
    return service.prepare([row["set_id"]])


def test_readonly_prepare_cleanup_preserves_remote_and_other_questions(cleanup_bank):
    service, bid = cleanup_bank
    seed_batch(service.root, 'listening')
    before, remote = db_files(service.root), pg_rows(service)
    p = plan(service)
    assert db_files(service.root) == before
    result = service.execute(p)
    assert result["complete"] and len(result["changed"]) == 50
    backup = Path(result["backup"])
    manifest = inspect_backup(backup.read_bytes())
    assert all(name in {i['path'] for i in manifest['files']} for name in before)
    assert len(collect_candidate_catalog(service.root).records) == 50
    after = db_files(service.root)
    assert all(after[k] == v for k, v in before.items() if not k.startswith('data/types/'))
    assert pg_rows(service) == remote
    assert service.candidates()[0]["complete"]
    assert batch_was_cleaned(service.root, bid)
    assert len([r for r in load_overview(service.root, service.reader.target_url)['rows'] if r['status'] == '로컬 정리 완료']) == 50
    with pytest.raises(ValueError):
        service.execute(p)
    with pytest.raises(ValueError, match='로컬 정리'):
        BulkRunner(service.root).start(batch_id=bid, media_config={'synthetic': True})
    restored = service.root / 'restored'
    restore_backup(restored, backup.read_bytes())
    assert len(collect_candidate_catalog(restored).records) == 100
    assert not cleanup_receipts(restored)


@pytest.mark.parametrize('kind', ['local_approval', 'set_approval', 'item_approval', 'media', 'slots', 'pipeline'])
def test_ineligible_sets_never_delete(cleanup_bank, kind):
    service, _ = cleanup_bank
    if kind == 'local_approval':
        with sqlite3.connect(service.root / 'data/types/grammar_blank.db') as c:
            c.execute("UPDATE reviews SET data_json='{}'")
    elif kind == 'pipeline':
        with sqlite3.connect(service.root / 'data/bulk_generation.db') as c:
            c.execute("UPDATE batches SET pipeline_status='image'")
    else:
        statements = {
            'set_approval': "UPDATE topik_bank.question_sets SET review_status='pending_review'",
            'item_approval': "UPDATE topik_bank.items SET review_status='pending_review' WHERE position=1",
            'media': "UPDATE topik_bank.item_visual_assets SET storage_url=''",
            'slots': "DELETE FROM topik_bank.items WHERE position=50",
        }
        with service.reader.connect() as c:
            c.execute(statements[kind])
    before = db_files(service.root)
    row = service.candidates()[0]
    assert row['reasons']
    with pytest.raises(ValueError):
        service.prepare([row['set_id']])
    assert db_files(service.root) == before
    assert not (service.root / 'backups').exists()


@pytest.mark.parametrize('phase', ['paused', 'failed'])
def test_old_stopped_media_attempt_does_not_block_registered_set(cleanup_bank, phase):
    from topik_question_lab.step_03_generation.bulk_generation import execution_lock
    service, _ = cleanup_bank
    with sqlite3.connect(service.root / 'data/bulk_generation.db') as c:
        c.execute("UPDATE batches SET pipeline_status=?", (phase,))
        c.execute("UPDATE media_tasks SET status='pending' WHERE kind='chart'")
    p = plan(service)
    before, remote = db_files(service.root), pg_rows(service)
    with execution_lock(service.root):
        with pytest.raises(ValueError):
            service.execute(p)
    assert db_files(service.root) == before
    result = service.execute(p)
    assert result['complete'] and len(result['changed']) == 50
    assert pg_rows(service) == remote


@pytest.mark.parametrize('kind', ['local_edit', 'remote_approval', 'backup_failure', 'offline'])
def test_stale_plan_or_failed_backup_cannot_delete(cleanup_bank, monkeypatch, kind):
    service, _ = cleanup_bank
    p = plan(service)
    if kind == 'local_edit':
        change_question(service.root, edit=lambda q: {**q, 'explanation': 'changed after confirmation'})
    elif kind == 'remote_approval':
        with service.reader.connect() as c:
            c.execute("UPDATE topik_bank.items SET review_status='pending_review' WHERE position=1")
    elif kind == 'backup_failure':
        monkeypatch.setattr(service, '_backup', lambda: (_ for _ in ()).throw(OSError('synthetic disk failure')))
    else:
        service = LocalSetCleanup(service.root, '')
    before = db_files(service.root)
    with pytest.raises(ValueError):
        service.execute(p)
    assert db_files(service.root) == before


def test_partial_commit_is_recoverable_and_uncommitted_files_rollback(cleanup_bank, monkeypatch):
    import topik_question_lab.step_06_publication.local_set_cleanup as module
    service, _ = cleanup_bank
    p = plan(service)
    original = sqlite3.connect
    class FailCommit:
        def __init__(self, c): self.c = c
        def __getattr__(self, name): return getattr(self.c, name)
        def commit(self): raise sqlite3.OperationalError('synthetic commit failure')
    def connect(path, *args, **kwargs):
        c = original(path, *args, **kwargs)
        return FailCommit(c) if 'grammar_blank.db?mode=rw' in str(path) else c
    monkeypatch.setattr(module.sqlite3, 'connect', connect)
    result = service.execute(p)
    assert not result['complete'] and 0 < len(result['changed']) < 50
    assert Path(result['backup']).is_file()
    assert len(collect_candidate_catalog(service.root).records) == 50 - len(result['changed'])
    monkeypatch.setattr(module.sqlite3, 'connect', original)
    row = service.candidates()[0]
    assert not row['reasons']
    result2 = service.execute(service.prepare([row['set_id']]))
    assert result2['complete'] and len(result2['changed']) + len(result['changed']) == 50
    assert service.candidates()[0]['complete']


def test_ui_requires_confirmation_and_refreshes_result(cleanup_bank):
    service, _ = cleanup_bank
    before = db_files(service.root)
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    next(b for b in app.button if b.label == '등록 완료 세트 로컬 정리').click().run()
    assert not app.exception and db_files(service.root) == before
    sid = service.candidates()[0]['set_id']
    next(w for w in app.multiselect if w.label == '로컬 정리할 세트').set_value([sid]).run()
    next(b for b in app.button if b.label == '선택 세트 정리 내용 확인').click().run()
    execute = next(b for b in app.button if b.label == '백업 후 50문제 로컬 정리하기')
    assert execute.disabled and not app.exception and db_files(service.root) == before
    next(w for w in app.checkbox if w.key == 'set-cleanup-confirm').check().run()
    next(b for b in app.button if b.label == '백업 후 50문제 로컬 정리하기').click().run()
    assert not app.exception
    assert any('로컬 50문제 정리 완료' in s.value for s in app.success)
    assert not collect_candidate_catalog(service.root).records


def test_reading_and_listening_multiple_sets_require_all_media(cleanup_bank, monkeypatch):
    service, _ = cleanup_bank
    bid = seed_media(service.root, 'listening')
    pipeline(service.root, bid, registration=MediaRegistration(service.root, service.reader.target_url)).run()
    for path in (service.root / 'data/listening/types').glob('*.db'):
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute("UPDATE reviews SET data_json=?", (json.dumps({'approved': True}),))
    with sqlite3.connect(service.root / 'data/bulk_generation.db') as c:
        c.execute("UPDATE batches SET pipeline_status='succeeded',auto_resume=0")
    with service.reader.connect() as c:
        c.execute("UPDATE topik_bank.items SET review_status='reviewed'")
        c.execute("UPDATE topik_bank.question_sets SET review_status='reviewed'")
        c.execute("UPDATE topik_bank.question_set_item_audio_bindings SET is_current=false WHERE position=1")
    rows = service.candidates()
    assert next(r for r in rows if r['section'] == 'listening')['reasons']
    before = db_files(service.root)
    with pytest.raises(ValueError):
        service.prepare([r['set_id'] for r in rows])
    assert db_files(service.root) == before
    with service.reader.connect() as c:
        c.execute("UPDATE topik_bank.question_set_item_audio_bindings SET is_current=true WHERE position=1")
    def forbidden(*args, **kwargs):
        raise AssertionError('No S3 requests during cleanup')
    monkeypatch.setattr('topik_question_lab.step_06_publication.media_cloud.S3MediaStore.__init__', forbidden)
    remote = pg_rows(service)
    result = service.execute(service.prepare([r['set_id'] for r in service.candidates()]))
    assert result['complete'] and len(result['changed']) == 100
    assert all(r['complete'] for r in service.candidates())
    assert pg_rows(service) == remote


def test_backup_integrity_failure_and_lost_commit_acknowledgement(cleanup_bank, monkeypatch):
    import topik_question_lab.step_06_publication.local_set_cleanup as module
    service, _ = cleanup_bank
    p = plan(service)
    before = db_files(service.root)
    inspect = module.inspect_backup
    monkeypatch.setattr(module, 'inspect_backup', lambda _: (_ for _ in ()).throw(ValueError('synthetic corrupt backup')))
    with pytest.raises(ValueError):
        service.execute(p)
    assert db_files(service.root) == before
    monkeypatch.setattr(module, 'inspect_backup', inspect)
    original = sqlite3.connect
    first = sorted((service.root / 'data/types').glob('*.db'))[0].as_uri() + '?mode=rw'
    class LostAck:
        def __init__(self, c): self.c = c
        def __getattr__(self, name): return getattr(self.c, name)
        def commit(self):
            self.c.commit()
            raise sqlite3.OperationalError('synthetic acknowledgement loss')
    def connect(path, *args, **kwargs):
        c = original(path, *args, **kwargs)
        return LostAck(c) if str(path) == first else c
    monkeypatch.setattr(module.sqlite3, 'connect', connect)
    with pytest.raises(ValueError):
        service.execute(p)
    monkeypatch.setattr(module.sqlite3, 'connect', original)
    row = service.candidates()[0]
    assert 0 < row['count'] < 50 and not row['reasons']
    result = service.execute(service.prepare([row['set_id']]))
    assert result['complete'] and service.candidates()[0]['complete']


def test_unavailable_view_is_readonly(tmp_path):
    before = files(tmp_path)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    next(b for b in app.button if b.label == '등록 완료 세트 로컬 정리').click().run()
    assert not app.exception and files(tmp_path) == before
