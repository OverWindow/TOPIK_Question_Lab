from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.local_script_runner import LocalScriptRunner

from topik_question_lab.menus.bulk_generation.bulk_progress import completed_batches, needs_progress_refresh


PAGE = Path(__file__).resolve().parents[1] / "topik_question_lab/menus/bulk_generation/bulk_generation_app.py"


def snapshot():
    batches, items, sets, rows = [], [], [], []
    for section in ('reading', 'listening'):
        batches.append({'id': section, 'set_id': section, 'section': section, 'model': 'deepseek-flash',
                        'status': 'succeeded', 'pipeline_status': 'paused' if section == 'listening' else '',
                        'created_at': '2026-11-02 00:00:00', 'review_status': '승인',
                        'coverage': {'audio_bound': 50 if section == 'listening' else 0,
                                     'audio_required': 50 if section == 'listening' else 0,
                                     'images_ready': 12 if section == 'listening' else 1,
                                     'images_required': 12 if section == 'listening' else 1},
                        'media_config': '{}', 'preset': None, 'error': '', 'auto_resume': 0})
        sets.append({'set_id': section, 'review_status': 'reviewed'})
        items.extend({'set_id': section, 'position': n, 'review_status': 'reviewed'} for n in range(1, 51))
        rows.extend({'set_id': section, 'status': '내용 일치'} for _ in range(50))
    return {'bank': {'available': True, 'media_available': True, 'sets': sets, 'items': items},
            'batches': batches, 'rows': rows, 'errors': []}


def test_completed_registration_overrides_old_local_pipeline_status():
    data = snapshot()
    before = deepcopy(data)
    assert {b['id'] for b in completed_batches(data)} == {'reading', 'listening'}
    assert data == before


@pytest.mark.parametrize('kind', ['audio', 'image', 'position', 'question', 'changed', 'missing_local', 'running', 'media_running', 'retired'])
def test_incomplete_or_active_batch_remains_visible(kind):
    data = snapshot()
    if kind == 'audio': data['batches'][1]['coverage']['audio_bound'] = 49
    if kind == 'image': data['batches'][1]['coverage']['images_ready'] = 11
    if kind == 'position': data['bank']['items'][-1]['position'] = 49
    if kind == 'question': data['bank']['items'].pop()
    if kind == 'changed': data['rows'][-1]['status'] = '내용 불일치'
    if kind == 'missing_local': data['rows'][-1]['status'] = '로컬 원본 없음'
    if kind == 'running': data['batches'][1]['status'] = 'running'
    if kind == 'media_running': data['batches'][1]['pipeline_status'] = 'image'
    if kind == 'retired': data['bank']['sets'][1]['review_status'] = 'retired'
    assert [b['id'] for b in completed_batches(data)] == ['reading']


@pytest.mark.parametrize('kind', ['connection', 'schema', 'local_error'])
def test_unknown_completion_keeps_all_batches(kind):
    data = snapshot()
    if kind == 'connection': data['bank']['available'] = False
    if kind == 'schema': data['bank']['media_available'] = False
    if kind == 'local_error': data['errors'] = ['synthetic unreadable file']
    assert not completed_batches(data)


def test_pending_review_and_cleaned_originals_are_completed_history():
    data = snapshot()
    for s in data['bank']['sets']: s['review_status'] = 'pending_review'
    for i in data['bank']['items']: i['review_status'] = 'pending_review'
    for r in data['rows']: r['status'] = '로컬 정리 완료'
    assert len(completed_batches(data)) == 2


@pytest.fixture
def progress_page(tmp_path, monkeypatch):
    import topik_question_lab.common.generation.runner_service as runners
    import topik_question_lab.common.generation.bulk_generation as bulk
    import topik_question_lab.common.question_bank.storage_overview as overview
    data = snapshot()
    original = deepcopy(data['batches'])
    calls = []
    timers = []
    forward_msgs = LocalScriptRunner.forward_msgs

    def capture_timers(script_runner):
        messages = forward_msgs(script_runner)
        timers.append([m.auto_rerun.interval for m in messages if m.HasField('auto_rerun')])
        return messages

    monkeypatch.setattr(LocalScriptRunner, 'forward_msgs', capture_timers)
    class Store:
        def settings(self): return {}
        def set_setting(self, *args): pass
        def batches(self): return original
        def tasks(self, bid):
            return [{'count': 50, 'review_count': 0, 'snapshot': {'slots': list(range(1, 51))},
                     'type_id': 'synthetic', 'status': 'succeeded', 'attempts': 1, 'error': ''}]
    runner = SimpleNamespace(store=Store(), thread=None, recover_idle=lambda: None)
    monkeypatch.setattr(runners, 'get_runner', lambda _: runner)
    monkeypatch.setattr(bulk, 'prepare_batch', lambda *args: ([], ['synthetic no generation']))
    monkeypatch.setattr(bulk, 'model_options', lambda _: [('deepseek_v4_1_flash', 'deepseek-flash')])
    def read(*args):
        calls.append(True)
        return deepcopy(data)
    monkeypatch.setattr(overview, 'load_overview', read)
    app = AppTest.from_file(str(PAGE), default_timeout=20).run()
    return SimpleNamespace(app=app, data=data, batches=original, runner=runner, calls=calls, timers=timers)


def test_page_hides_completed_and_refresh_restores_incomplete_work(progress_page):
    app, data, original, calls = progress_page.app, progress_page.data, progress_page.batches, progress_page.calls
    assert not app.exception
    assert not any(s.label == '생성 묶음' for s in app.selectbox)
    history = next(e for e in app.expander if e.label == '완료된 생성 이력 · 2개')
    assert not history.proto.expanded
    history_picker = next(s for s in app.selectbox if s.label == '완료된 생성 묶음')
    assert all('2026/11/02 09:00 생성' in label for label in history_picker.options)
    assert any('식별코드 READING' in c.value for c in app.caption)
    assert any('미완료인 생성 작업이 없습니다' in m.value for m in app.info)
    assert not any(b.key == 'bulk-media' for b in app.button)
    app.run()
    assert len(calls) == 1
    data['batches'][1]['coverage']['images_ready'] = 11
    next(b for b in app.button if b.key == 'bulk-refresh-registration').click().run()
    assert not app.exception
    picker = next(s for s in app.selectbox if s.label == '생성 묶음')
    assert picker.value == 'listening' and len(picker.options) == 1
    assert 'LISTENIN' in picker.options[0]
    data['batches'][1]['coverage']['images_ready'] = 12
    next(b for b in app.button if b.key == 'bulk-refresh-registration').click().run()
    assert not app.exception and not any(s.label == '생성 묶음' for s in app.selectbox)
    assert original == snapshot()['batches']


def test_idle_page_has_no_timer_and_keeps_snapshot_until_manual_refresh(progress_page):
    app = progress_page.app
    assert not app.exception and not progress_page.timers[-1]
    identity, checked_at, cached = app.session_state['bulk-registration-snapshot']
    app.session_state['bulk-registration-snapshot'] = (identity, checked_at - 60, cached)
    app.run()
    assert not app.exception and not progress_page.timers[-1]
    assert len(progress_page.calls) == 1
    app.button(key='bulk-refresh-registration').click().run()
    assert not app.exception and len(progress_page.calls) == 2


@pytest.mark.parametrize('terminal', ['succeeded', 'paused', 'failed'])
def test_active_page_polls_then_refreshes_once_and_stops(progress_page, terminal):
    app, batch = progress_page.app, progress_page.batches[0]
    batch.update(status='running', pipeline_status='retry_wait', auto_resume=1)
    app.run()
    assert not app.exception and 2.0 in progress_page.timers[-1]
    before = len(progress_page.calls)
    identity, checked_at, cached = app.session_state['bulk-registration-snapshot']
    app.session_state['bulk-registration-snapshot'] = (identity, checked_at - 60, cached)
    app.run()
    assert not app.exception and len(progress_page.calls) == before + 1

    batch.update(status='succeeded' if terminal == 'succeeded' else 'interrupted',
                 pipeline_status=terminal, auto_resume=0)
    before = len(progress_page.calls)
    app.run()
    assert not app.exception and not progress_page.timers[-1]
    assert len(progress_page.calls) == before + 1
    app.run()
    assert len(progress_page.calls) == before + 1

    if terminal != 'succeeded':
        def resume(*, batch_id):
            assert batch_id == batch['id']
            batch.update(status='running', pipeline_status='generating', auto_resume=1)

        progress_page.runner.start = resume
        # Show the unfinished local batch in the next bank snapshot.
        progress_page.data['batches'][0].update(status='interrupted', pipeline_status=terminal)
        progress_page.data['batches'][0]['coverage']['images_ready'] = 0
        app.button(key='bulk-refresh-registration').click().run()
        app.button(key='bulk-resume').click().run()
        assert not app.exception and 2.0 in progress_page.timers[-1]


def test_worker_exit_stops_timer_even_without_a_batch_status_change(progress_page):
    app = progress_page.app
    progress_page.runner.thread = SimpleNamespace(is_alive=lambda: True)
    app.run()
    assert not app.exception and 2.0 in progress_page.timers[-1]
    before = len(progress_page.calls)
    progress_page.runner.thread = None
    app.run()
    assert not app.exception and not progress_page.timers[-1]
    assert len(progress_page.calls) == before + 1


def test_retry_wait_needs_auto_resume_and_terminal_states_are_idle():
    batch = {'status': 'succeeded', 'pipeline_status': 'retry_wait', 'auto_resume': 1}
    assert needs_progress_refresh([batch])
    assert not needs_progress_refresh([{**batch, 'auto_resume': 0}])
    for phase in ('succeeded', 'paused', 'failed', 'ready', 'needs_attention'):
        assert not needs_progress_refresh([{**batch, 'pipeline_status': phase}])
    assert not needs_progress_refresh([])
    assert needs_progress_refresh([], runner_active=True)
