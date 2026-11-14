"""Set browsing and explicit deletion; question edits use the existing editor."""
from collections import Counter

import streamlit as st

from topik_question_lab.common.question_bank.connection import digest
from topik_question_lab.common.question_bank.set_deletion import SOURCE, SetDeletion, configured_banks
from topik_question_lab.common.ui.set_identity import SECTION, korean_time


def deletion_result(job):
    counts = Counter(f['state'] for f in job['files'])
    text = f"파일 삭제 {counts['deleted']}개 · 공유 파일 보존 {counts['preserved']}개 · 남은 파일 {counts['pending']}개"
    if job['status'] == 'complete':
        st.success('세트 삭제 처리가 완료되었습니다. ' + text)
    else:
        st.warning('삭제가 일부 완료됐습니다. 아래 ‘남은 삭제 이어서 처리’에서 재시도하세요. ' + text)
    if job.get('error'):
        st.error(job['error'])


def render_set_browser(bank, views, editor, root, refresh):
    service = SetDeletion(root, configured_banks(editor.url))
    result = st.session_state.pop('bank-set-delete-result', None)
    if result:
        deletion_result(result)
    pending = service.pending()
    if pending:
        with st.expander('남은 삭제 이어서 처리', expanded=True):
            for job in pending:
                st.write(f"{korean_time(job['created_at'])} · 세트 {job['set_id'].replace('-', '')[:8].upper()}")
                st.caption('DB: ' + ' · '.join(f"{label}: {'완료' if state['state'] == 'done' else '미완료'}"
                           for label, state in job['databases'].items()))
                for error in sorted({f.get('error') for f in job['files'] if f.get('error')}):
                    st.caption(error)
                if job.get('error'):
                    st.error(job['error'])
                same_config = job['config'] == service.config
                if not same_config:
                    st.warning('DB 설정이 달라졌습니다. 작업 당시 연결 설정을 복원한 뒤 이어서 처리하세요.')
                if st.button('남은 삭제 이어서 처리', key='resume-set-delete-' + job['id'], disabled=not same_config):
                    with st.spinner('공유 여부를 다시 확인하고 남은 삭제를 처리합니다.'):
                        st.session_state['bank-set-delete-result'] = service.resume(job['id'])
                    refresh()
                    st.rerun()
    st.subheader('세트 목록')
    left, right = st.columns(2)
    exam = left.selectbox('세트 시험 형식', ['all', 'pbt', 'ibt'], format_func=lambda x: '전체' if x == 'all' else x.upper())
    section = right.selectbox('세트 영역', ['all', 'reading', 'listening'], format_func=lambda x: SECTION.get(x, '전체'))
    catalog = {sid: view for sid, view in views.items()
               if (exam == 'all' or view.get('exam_format', 'pbt') == exam)
               and (section == 'all' or view['section'] == section)}
    ordered = sorted(catalog, key=lambda sid: (str(catalog[sid].get('generation_created_at') or catalog[sid].get('published_at') or ''), sid), reverse=True)
    display_rows = [{'생성일시(KST)': korean_time(catalog[sid].get('generation_created_at')),
                     '시험': catalog[sid].get('exam_format', 'pbt').upper(), '영역': SECTION.get(catalog[sid]['section']),
                     '모델': catalog[sid]['model'], '세트 번호': catalog[sid].get('set_sequence'),
                     '문항 수': catalog[sid]['count'], '상태': catalog[sid]['review'], '식별코드': catalog[sid]['code'],
                     '_set_id': sid} for sid in ordered]
    table = st.session_state.get('bank-editor-set-table')
    previous = st.session_state.get('bank-editor-current-set')
    if previous not in catalog or (table and table['target'] != editor.target):
        previous = None
    identity = digest((editor.target, display_rows))
    if not table or table['identity'] != identity:
        table = {'identity': identity, 'target': editor.target,
                 'rows': [{'선택': row['_set_id'] == previous, **row} for row in display_rows]}
        st.session_state['bank-editor-set-table'] = table
        st.session_state['bank-editor-set-table-version'] = st.session_state.get('bank-editor-set-table-version', 0) + 1
    chosen = None
    checked = []
    if display_rows:
        st.caption('편집할 세트 한 개를 체크하세요. 다른 세트를 체크하면 이전 선택이 해제됩니다.')
        edited = st.data_editor(table['rows'], hide_index=True, width='stretch', num_rows='fixed',
            disabled=list(display_rows[0]),
            column_config={'선택': st.column_config.CheckboxColumn('선택', required=True), '_set_id': None},
            key=f"bank-editor-set-table-{st.session_state['bank-editor-set-table-version']}")
        checked = [row['_set_id'] for row in edited if row['선택'] and row['_set_id'] in catalog]
        newly_checked = [sid for sid in checked if sid != previous]
        chosen = newly_checked[-1] if newly_checked else previous if previous in checked else None
    if st.session_state.get('bank-editor-current-set') != chosen:
        for key in ('bank-editor-choice', 'bank-editor-snapshot', 'bank-editor-draft', 'bank-editor-plan',
                    'bank-editor-table', 'bank-set-delete-plan'):
            st.session_state.pop(key, None)
        st.session_state['bank-editor-selected'] = []
        st.session_state['bank-editor-current-set'] = chosen
        # Rebuild the widget so only the active set is checked, also after edit confirmation.
        if any(row['선택'] != (row['_set_id'] == chosen) for row in table['rows']):
            table['rows'] = [{'선택': row['_set_id'] == chosen, **row} for row in display_rows]
            st.session_state['bank-editor-set-table-version'] += 1
            st.rerun()
    if chosen is None:
        st.info('표에서 세트를 체크한 뒤, 문항 표에서 수정할 문제를 체크하세요.')
        return None
    members = sorted([r for r in bank['items'] if str(r['set_id']) == chosen], key=lambda r: (r['position'] or 0, str(r['item_id'])))
    with st.expander('세트·문항·미디어 완전 삭제'):
        targets = st.multiselect('함께 삭제할 등록 서버', [label for label in service.banks if label != SOURCE], key='bank-set-delete-targets')
        st.caption('생성기 PostgreSQL은 항상 포함됩니다. 공유 파일 확인: ' + ' · '.join(service.banks))
        st.write('선택하지 않은 서버나 다른 세트가 사용하는 파일은 보존합니다. 사용 기록이 참조하는 세트는 삭제할 수 없습니다.')
        request = digest((service.config, chosen, sorted(targets)))
        saved = st.session_state.get('bank-set-delete-plan')
        if saved and saved[0] != request:
            st.session_state.pop('bank-set-delete-plan', None)
            saved = None
        if st.button('세트 삭제 내용 확인'):
            with st.spinner('모든 설정 서버의 세트와 파일 참조를 확인합니다.'):
                plan = service.prepare(chosen, targets)
            saved = (request, plan)
            st.session_state['bank-set-delete-plan'] = saved
        if saved:
            plan = saved[1]
            st.dataframe([{'대상': label, '세트': data['counts']['question_sets'], '문항': data['counts']['items'],
                           '음원 기록': data['counts']['tts_audio_assets'], '이미지 기록': data['counts']['item_visual_assets']}
                          for label, data in plan['databases'].items()], hide_index=True)
            counts = Counter(f['state'] for f in plan['files'])
            st.warning(f"실제 파일 삭제 {counts['pending']}개 · 공유 파일 보존 {counts['preserved']}개. 삭제는 되돌릴 수 없습니다.")
            prefix = 'confirm-set-delete-' + plan['id']
            code = st.text_input('삭제할 세트 식별코드 입력', key=prefix + '-code')
            confirmed = st.checkbox('선택한 서버의 세트·문항과 공유되지 않는 미디어 파일을 영구 삭제합니다', key=prefix)
            if st.button('세트와 미디어 영구 삭제', type='primary',
                         disabled=not confirmed or code.strip().upper() != catalog[chosen]['code']):
                with st.spinner('세트와 미디어 삭제를 처리합니다.'):
                    st.session_state['bank-set-delete-result'] = service.execute(plan)
                st.session_state.pop('bank-set-delete-plan', None)
                refresh()
                st.rerun()
    if not members:
        st.info('문항이 없는 세트입니다. 세트 전체 삭제는 위에서 실행할 수 있습니다.')
        return None
    return members
