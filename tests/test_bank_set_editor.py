from pathlib import Path
import json

import pytest
from streamlit.testing.v1 import AppTest

from test_bank_editor import bank, pick, seed_listening, select_table_questions
from test_bank_transfer import data_editor_widget_events
from test_set_deletion import FakeDeleter
from topik_question_lab.common.question_bank.connection import digest
from topik_question_lab.menus.bank_transfer.bank_transfer import _all_rows


PAGE = Path(__file__).resolve().parents[1] / 'topik_question_lab/menus/bank_editor/bank_editor_app.py'


def select_set(app, set_id):
    table = next(frame for frame in app.dataframe if '_set_id' in frame.value.columns)
    states = app._tree.get_widget_states()
    edit = next(state for state in states.widgets if state.id == table.proto.id)
    # Checking another row leaves the old check in the browser until the app clears it.
    edits = ({str(index): {'선택': False} for index in table.value.index} if set_id is None else
             {str(next(index for index, sid in enumerate(table.value['_set_id']) if sid == set_id)): {'선택': True}})
    edit.string_value = json.dumps({'edited_rows': edits, 'added_rows': [], 'deleted_rows': []})
    app._run(states)
    assert not app.exception and not app.error
    table = next(frame for frame in app.dataframe if '_set_id' in frame.value.columns)
    assert table.value.loc[table.value['선택'], '_set_id'].tolist() == ([set_id] if set_id else [])
    assert app.session_state['bank-editor-current-set'] == set_id


def set_page(bank):
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    app.radio(key='bank-editor-mode').set_value('세트별').run()
    assert not app.exception and not app.error
    sid = pick(bank)['set']['set_id']
    assert not any(w.label == '편집할 세트' for w in app.selectbox)
    select_set(app, sid)
    assert not app.exception and not app.error
    return app, sid


def test_set_browser_edits_in_number_order_and_keeps_existing_item_identity(bank):
    original = pick(bank)
    app, sid = set_page(bank)
    assert not app.text_area and app.session_state['bank-editor-selected'] == []
    third = pick(bank, 3)['item']['item_id']
    selected = [original['item']['item_id'], third]
    select_table_questions(app, selected)
    assert app.session_state['bank-editor-choice'] == original['item']['item_id']
    assert app.session_state['bank-editor-selected'] == selected
    next(w for w in app.text_area if w.label == '해설').set_value('세트에서 수정한 해설')
    next(b for b in app.button if b.label == '변경 내용 확인').click().run()
    assert pick(bank)['item'] == original['item']
    next(b for b in app.button if b.label == '편집으로 돌아가기').click().run()
    table = next(frame for frame in app.dataframe if '_set_id' in frame.value.columns).value
    assert table.loc[table['선택'], '_set_id'].tolist() == [sid]
    assert app.session_state['bank-editor-selected'] == selected
    next(b for b in app.button if b.label == '변경 내용 확인').click().run()
    next(b for b in app.button if b.label == 'PostgreSQL에 저장').click().run()
    assert not app.exception and not app.error
    updated = pick(bank)
    assert updated['item']['explanation'] == '세트에서 수정한 해설'
    assert updated['item']['item_id'] == original['item']['item_id'] and updated['item']['set_id'] == sid
    assert app.session_state['bank-editor-selected'] == selected
    app.button(key='bank-editor-next').click().run()
    assert app.session_state['bank-editor-choice'] == third
    select_table_questions(app, [selected[0]])
    assert app.session_state['bank-editor-choice'] == selected[0]
    select_table_questions(app, [])
    assert not app.text_area and app.session_state['bank-editor-current-set'] == sid
    select_set(app, None)
    assert not app.text_area


def test_set_deletion_requires_code_and_confirmation_and_refreshes_list(bank, monkeypatch):
    import topik_question_lab.common.question_bank.set_deletion as deletion
    files = FakeDeleter()
    monkeypatch.setattr(deletion, 'MediaDeleter', lambda root: files)
    with bank.connect(True) as c:
        before = digest(_all_rows(c))
    app, sid = set_page(bank)
    next(b for b in app.button if b.label == '세트 삭제 내용 확인').click().run()
    assert not app.exception and not app.error
    button = next(b for b in app.button if b.label == '세트와 미디어 영구 삭제')
    assert button.disabled
    next(w for w in app.text_input if w.label == '삭제할 세트 식별코드 입력').set_value(sid.replace('-', '')[:8].upper()).run()
    assert next(b for b in app.button if b.label == '세트와 미디어 영구 삭제').disabled
    with bank.connect(True) as c:
        assert digest(_all_rows(c)) == before
    next(w for w in app.checkbox if '공유되지 않는 미디어' in w.label).check().run()
    next(b for b in app.button if b.label == '세트와 미디어 영구 삭제').click().run()
    assert not app.exception and not app.error
    with bank.connect(True) as c:
        assert not _all_rows(c)['items'] and not _all_rows(c)['question_sets']
    assert app.session_state['bank-editor-current-set'] is None
    assert any('삭제 처리가 완료' in s.value for s in app.success)


def test_checking_another_set_switches_editor_and_clears_previous_delete_plan(bank, tmp_path, monkeypatch):
    import topik_question_lab.common.question_bank.set_deletion as deletion
    monkeypatch.setattr(deletion, 'MediaDeleter', lambda root: FakeDeleter())
    seed_listening(bank, tmp_path)
    with bank.connect(True) as c:
        before = digest(_all_rows(c))
    app, first = set_page(bank)
    select_table_questions(app, [pick(bank)['item']['item_id']])
    second = pick(bank, section='listening')['set']['set_id']
    next(b for b in app.button if b.label == '세트 삭제 내용 확인').click().run()
    assert 'bank-set-delete-plan' in app.session_state
    select_set(app, second)
    assert 'bank-set-delete-plan' not in app.session_state
    assert app.session_state['bank-editor-selected'] == [] and not app.text_area
    second_item = pick(bank, section='listening')['item']['item_id']
    select_table_questions(app, [second_item])
    assert app.session_state['bank-editor-choice'] == second_item
    assert not any(b.label == '세트와 미디어 영구 삭제' for b in app.button)
    select_set(app, first)
    assert app.session_state['bank-editor-selected'] == [] and not app.text_area
    with bank.connect(True) as c:
        assert digest(_all_rows(c)) == before


def test_set_checkbox_survives_refresh_reordering_and_clears_when_filtered(bank, tmp_path, monkeypatch):
    import topik_question_lab.common.question_bank.direct_publication as publication
    seed_listening(bank, tmp_path)
    # Exercise the fallback order used when local generation records are unavailable.
    monkeypatch.setattr(publication, 'read_batches', lambda root: [])
    app, first = set_page(bank)
    selected = [pick(bank)['item']['item_id'], pick(bank, 3)['item']['item_id']]
    select_table_questions(app, selected)
    previous_order = next(frame for frame in app.dataframe if '_set_id' in frame.value.columns).value['_set_id'].tolist()
    with bank.connect() as c:
        c.execute("UPDATE topik_bank.question_sets SET published_at='2099-01-01' WHERE set_id=%s", (first,))
    next(b for b in app.button if b.label == '문제 목록 새로고침').click().run()
    assert not app.exception and not app.error
    table = next(frame for frame in app.dataframe if '_set_id' in frame.value.columns).value
    assert table['_set_id'].tolist() != previous_order and table['_set_id'].tolist()[0] == first
    assert table.loc[table['선택'], '_set_id'].tolist() == [first]
    assert app.session_state['bank-editor-selected'] == selected
    next(w for w in app.selectbox if w.label == '세트 영역').set_value('listening').run()
    assert not app.exception and not app.error
    assert app.session_state['bank-editor-current-set'] is None and not app.text_area
    assert app.session_state['bank-editor-selected'] == []
    assert not next(frame for frame in app.dataframe if '_set_id' in frame.value.columns).value['선택'].any()
