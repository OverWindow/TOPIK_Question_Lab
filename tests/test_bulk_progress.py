from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.local_script_runner import LocalScriptRunner

from topik_question_lab.menus.bulk_generation.bulk_progress import batch_review_details, completed_batches, needs_progress_refresh


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


@pytest.mark.parametrize("state", ["text_only", "text_failed", "media_failed", "media_completed"])
def test_one_resume_button_finishes_remaining_work(progress_page, monkeypatch, state):
    import json
    import topik_question_lab.common.media.media_generation as media

    app, batch = progress_page.app, progress_page.batches[0]
    config = {"settings": {"tts_delay": 9}, "target": "original-target"}
    calls, preflights = [], []
    progress_page.data["batches"][0]["coverage"]["images_ready"] = 0
    batch["status"] = "partial" if state == "text_failed" else "succeeded"
    if state.startswith("media"):
        batch.update(media_config=json.dumps(config), next_attempt_at=0,
                     pipeline_status="succeeded" if state == "media_completed" else "failed")
        progress_page.runner.store.media_tasks = lambda _: []
    monkeypatch.setattr(media, "preflight", lambda root, settings, section, url, exam:
                        preflights.append((section, exam)) or config)
    progress_page.runner.start = lambda **kwargs: calls.append(kwargs)
    app.button(key="bulk-refresh-registration").click().run()
    assert not app.exception and app.button(key="bulk-resume").label == "이어서 완료"
    assert not any(b.key in {"bulk-media", "bulk-open-publication"} for b in app.button)
    app.button(key="bulk-resume").click().run()
    assert not app.exception
    assert calls == [{"batch_id": "reading", "media_config": config}]
    assert preflights == ([] if state.startswith("media") else [("reading", batch.get("exam_format", "pbt"))])


def test_completed_set_opens_server_transfer_with_selection(progress_page, monkeypatch):
    from topik_question_lab.common.paths import PACKAGE_ROOT
    from topik_question_lab.common.ui.navigation import menu_page
    import topik_question_lab.menus.bank_transfer.bank_transfer as transfer

    reads = []
    monkeypatch.setenv("DATABASE_URL", "synthetic-source")
    monkeypatch.setattr(transfer, "list_source_sets", lambda url: reads.append(url) or [
        {"set_id": "reading", "section": "reading", "model": "deepseek-flash", "sequence": 1, "status": "reviewed", "items": 50}])
    import topik_question_lab.maintenance.startup as startup
    monkeypatch.setattr(startup, "prepare_local_database", lambda *args: None)
    app = AppTest.from_file(str(PACKAGE_ROOT / "main.py"), default_timeout=30)
    app.switch_page(menu_page("bulk_generation_app.py").relative_to(PACKAGE_ROOT).as_posix()).run()
    assert not app.exception
    app.button(key="bulk-transfer").click().run()
    assert not app.exception
    assert app.title[0].value == "서버에 문제 등록"
    assert app.session_state["bank-transfer-selected"] == ["reading"]
    table = app.dataframe[0].value
    assert table.loc[table["_set_id"] == "reading", "선택"].item()
    assert reads == ["synthetic-source"]


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
        import json
        config = {"settings": {"tts_delay": 9}, "target": "original-target"}
        batch["media_config"] = json.dumps(config)
        progress_page.runner.store.media_tasks = lambda _: []
        batch["next_attempt_at"] = 0

        def resume(*, batch_id, media_config):
            assert batch_id == batch['id']
            assert media_config == config
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


@pytest.mark.parametrize("section,type_id,slot", [
    ("reading", "ibt_chart_content_match", 7), ("listening", "ibt_visual_chart", 2),
])
def test_bulk_review_button_opens_exact_question_and_refreshes_after_save(tmp_path, monkeypatch, section, type_id, slot):
    from test_ibt_pipeline import generated_batch
    from topik_question_lab.common.generation.bulk_generation import storage_for
    from topik_question_lab.common.models import ProviderResult
    from topik_question_lab.common.paths import PACKAGE_ROOT
    from topik_question_lab.common.ui.navigation import menu_page
    import topik_question_lab.common.generation.runner_service as runners
    import topik_question_lab.common.generation.bulk_generation as bulk

    runner, bid, snapshots, _ = generated_batch(tmp_path, section, monkeypatch)
    for saved in snapshots:
        current = storage_for(tmp_path, section, saved["type_id"])
        for row in current.list_generated():
            current.save_validation(row["id"], [])
    storage = storage_for(tmp_path, section, type_id)
    item = storage.list_generated()[0]
    question = deepcopy(item["question"])
    if section == "reading":
        question["passage"] = question["passage"].replace("인구:", "인구(막대):").replace("고령 비율:", "고령 비율(꺾은선):")
        issue = {"code": "visual_material", "message": "시각 자료 확인 필요: 복합 도표 계열 이름이 지문에 없습니다.", "severity": "error"}
    else:
        question["choices"] = ["같은 제목"] * 4
        issue = {"code": "duplicate_choices", "message": "서로 같은 보기가 있습니다.", "severity": "error"}
    storage.save_generated_edit(item["id"], question)
    storage.save_validation(item["id"], [issue])
    other = {**item["question"], "stem": "별개의 자료를 다루는 다른 문항입니다.",
             "passage": "별개의 자료를 다루는 다른 문항입니다."}
    result = ProviderResult(provider=item["provider"], model=item["model"], operation="generation")
    storage.save_generation_result(result, "", "", type_id, "standard", [other],
                                   [[{"code": "similar_duplicate", "message": "다른 묶음의 경고", "severity": "warning"}]], "other-batch")
    assert storage.list_generated()[0]["id"] != item["id"]
    runner.store.update_task(bid, type_id, "succeeded", count=1, review_count=1, run_id=item["run_id"])
    monkeypatch.setenv("CHATKHU_API_KEY", "")
    monkeypatch.setattr(runners, "get_runner", lambda _: runner)
    monkeypatch.setattr(bulk, "prepare_batch", lambda *args: ([], ["synthetic no generation"]))
    before = storage.list_generated()
    details = batch_review_details(tmp_path, bid)
    assert details["counts"][type_id] == 1
    assert [(q["id"], q["slot"]) for q in details["questions"]] == [(item["id"], slot)]
    assert storage.list_generated() == before
    assert not batch_review_details(tmp_path, "missing-batch")["questions"]

    app = AppTest.from_file(str(PACKAGE_ROOT / "main.py"), default_timeout=30)
    bulk_page = menu_page("bulk_generation_app.py").relative_to(PACKAGE_ROOT).as_posix()
    app.switch_page(bulk_page).run()
    assert not app.exception
    if section == "reading":
        assert any("‘인구:’" in e.value and "‘고령 비율:’" in e.value and "작성 예:" in e.value for e in app.error)
    else:
        assert any("듣기 2번 · 오류: 서로 같은 보기가 있습니다." in e.value for e in app.error)
    assert "검사 지적 문항 1" in app.get("progress")[0].proto.text
    next(b for b in app.button if b.label == f"{slot}번 검수하기").click().run()
    assert not app.exception
    assert app.sidebar.radio(key=f"{section}-exam-format").value == "ibt"
    assert app.sidebar.radio(key=f"{section}-work-stage").value == "5. 검수·비교"
    selector_key = f"generated-review-selection-{type_id}" if section == "reading" else f"listening-generated-review-selection-{type_id}"
    assert app.selectbox(key=selector_key).value == item["id"]
    # AppTest retains the previous page hash/widgets after a server-side switch.
    review_page = menu_page("app.py" if section == "reading" else "listening_app.py")
    app.switch_page(review_page.relative_to(PACKAGE_ROOT).as_posix())._run()
    assert not app.exception and app.selectbox(key=selector_key).value == item["id"]
    if section == "reading":
        # Save the unchanged chart data with corrected passage headings.
        passage = next(w for w in app.text_area if w.value == question["passage"])
        passage.set_value(item["question"]["passage"])
        next(b for b in app.button if b.label == "문제 수정 저장").click().run()
    else:
        next(b for b in app.button if b.label == "수정·평가 저장하고 다음 문항").click().run()
    assert not app.exception
    assert not next(row for row in storage.list_generated() if row["id"] == item["id"])["validation"], [e.value for e in app.error]
    app.switch_page(bulk_page).run()
    assert not app.exception
    assert not any(b.label == f"{slot}번 검수하기" for b in app.button), batch_review_details(tmp_path, bid)["questions"]
    assert "검사 지적 문항 0" in app.get("progress")[0].proto.text
    table = next(frame.value for frame in app.dataframe if "검사 지적" in frame.value.columns)
    assert table.loc[table["유형"] == type_id, "검사 지적"].item() == 0
    assert next(t for t in runner.store.tasks(bid) if t["type_id"] == type_id)["review_count"] == 1


def test_bulk_review_distinguishes_warnings_and_keeps_multiple_diagnostics(progress_page, monkeypatch):
    import topik_question_lab.menus.bulk_generation.bulk_progress as progress
    progress_page.data["batches"][0]["coverage"]["images_ready"] = 0
    details = {"counts": {"synthetic": 1}, "approved": 50, "errors": [], "questions": [
        {"section": "reading", "type_id": "synthetic", "id": 42, "slot": 7, "issues": [
            {"severity": "warning", "message": "기존 지문과 유사합니다."},
            {"severity": "error", "message": "지문에 필요한 수치가 없습니다."}]}]}
    monkeypatch.setattr(progress, "batch_review_details", lambda *args: deepcopy(details))
    app = progress_page.app
    app.button(key="bulk-refresh-registration").click().run()
    assert not app.exception
    assert any("읽기 7번 · 경고: 기존 지문과 유사합니다." == w.value for w in app.warning)
    assert any("읽기 7번 · 오류: 지문에 필요한 수치가 없습니다." == e.value for e in app.error)
    assert "검사 지적 문항 1" in app.get("progress")[0].proto.text
    assert len([b for b in app.button if b.label == "7번 검수하기"]) == 1
