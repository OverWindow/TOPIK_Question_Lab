"""Explicit set deletion across configured banks, with durable manual retries."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
from urllib.parse import unquote, urlsplit

from psycopg import sql

from topik_question_lab.common.generation.bulk_generation import execution_lock
from topik_question_lab.common.media.media_deletion import MediaDeleter
from topik_question_lab.common.question_bank.connection import connection, database_identity, digest
from topik_question_lab.menus.bank_transfer.bank_transfer import TABLES, KEYS, _all_rows


SOURCE = '생성기'
ASSETS = ('tts_audio_assets', 'item_visual_assets')
FILE_FIELDS = ('storage_backend', 'storage_bucket', 'storage_path', 'storage_url', 'file_sha256', 'byte_size')


def configured_banks(source):
    return {label: url.strip() for label, url in {
        SOURCE: source, '로컬 서버': os.getenv('QUESTION_BANK_LOCAL_DATABASE_URL', ''),
        '운영 서버': os.getenv('QUESTION_BANK_PRODUCTION_DATABASE_URL', ''),
    }.items() if url.strip()}


def object_tokens(asset):
    url = urlsplit(asset.get('storage_url', ''))
    return {('object', asset['storage_backend'], asset['storage_bucket'], asset['storage_path']),
            ('url', url.scheme.lower(), url.netloc.lower(), unquote(url.path))}


def affected_rows(rows, set_id):
    items = [r for r in rows['items'] if r['set_id'] == set_id]
    ids = {r['item_id'] for r in items}
    bindings = [r for r in rows['item_audio_bindings'] if r['item_id'] in ids]
    audio_ids = {r['audio_asset_id'] for r in bindings}
    shared = {r['audio_asset_id'] for r in rows['item_audio_bindings'] if r['item_id'] not in ids}
    related = [r for r in rows['tts_audio_assets'] if r['audio_asset_id'] in audio_ids]
    images = [r for r in rows['item_visual_assets'] if r['item_id'] in ids]
    return {
        'question_sets': [r for r in rows['question_sets'] if r['set_id'] == set_id],
        'items': items, 'item_audio_bindings': bindings, 'item_visual_assets': images,
        'tts_audio_assets': [r for r in related if r['audio_asset_id'] not in shared],
    }, related + images


def check_references(c, deleted):
    constraints = c.execute("""SELECT p.relname,n.nspname,r.relname,
        array_agg(a.attname ORDER BY k.ord),array_agg(b.attname ORDER BY k.ord)
        FROM pg_constraint f JOIN pg_class p ON p.oid=f.confrelid
        JOIN pg_class r ON r.oid=f.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
        CROSS JOIN LATERAL unnest(f.conkey,f.confkey) WITH ORDINALITY AS k(local_key,parent_key,ord)
        JOIN pg_attribute a ON a.attrelid=f.conrelid AND a.attnum=k.local_key
        JOIN pg_attribute b ON b.attrelid=f.confrelid AND b.attnum=k.parent_key
        WHERE f.contype='f' AND p.relnamespace=to_regnamespace('topik_bank') AND p.relname=ANY(%s)
        GROUP BY f.oid,p.relname,n.nspname,r.relname""", (list(TABLES),)).fetchall()
    for parent, schema, child, local, remote in constraints:
        ids = [r[KEYS[parent][0]] for r in deleted[parent]]
        if not ids:
            continue
        condition = sql.SQL(' AND ').join(sql.SQL('r.{}=p.{}').format(sql.Identifier(a), sql.Identifier(b)) for a, b in zip(local, remote))
        query = sql.SQL('SELECT 1 FROM {} r JOIN topik_bank.{} p ON {} WHERE p.{}=ANY(%s::uuid[])').format(
            sql.Identifier(schema, child), sql.Identifier(parent), condition, sql.Identifier(KEYS[parent][0]))
        params = [ids]
        if schema == 'topik_bank' and child in TABLES:
            query += sql.SQL(' AND NOT (r.{}=ANY(%s::uuid[]))').format(sql.Identifier(KEYS[child][0]))
            params.append([r[KEYS[child][0]] for r in deleted[child]])
        if c.execute(query + sql.SQL(' LIMIT 1'), params).fetchone():
            raise ValueError('세트·문항·미디어를 참조하는 사용 기록이 있어 삭제할 수 없습니다.')


def _lock(c):
    # Block writers and new foreign-key references until file checks/deletion finish.
    c.execute(sql.SQL('LOCK TABLE {} IN EXCLUSIVE MODE').format(
        sql.SQL(',').join(sql.Identifier('topik_bank', table) for table in sorted(TABLES))))


class SetDeletion:
    def __init__(self, root, banks, *, deleter=None):
        self.root, self.banks = Path(root).resolve(), dict(banks)
        self.config = digest(self.banks)
        self.directory = self.root / 'data/set_deletions'
        self.deleter = deleter or MediaDeleter(self.root)

    def _path(self, job_id):
        return self.directory / (uuid.UUID(job_id).hex + '.json')

    def _save(self, job):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(job['id'])
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as f:
            json.dump(job, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)

    def pending(self):
        result = []
        for path in sorted(self.directory.glob('*.json')):
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['status'] != 'complete':
                result.append(job)
        return result

    def _read(self, connections, set_id, targets):
        inventories, deleted, related, identities = {}, {}, [], {}
        for label, c in connections.items():
            identities[label] = digest(database_identity(c))
            if identities[label] in list(identities.values())[:-1]:
                raise ValueError('서로 다른 서버 이름이 같은 DB를 가리킵니다. 연결 설정을 확인하세요.')
            inventories[label] = _all_rows(c)
            if label in targets:
                deleted[label], assets = affected_rows(inventories[label], set_id)
                related.extend(assets)
                check_references(c, deleted[label])
        return inventories, deleted, related, identities

    @staticmethod
    def _files(inventories, deleted, related):
        protected = set()
        for label, rows in inventories.items():
            for table in ASSETS:
                removed = {r[KEYS[table][0]] for r in deleted.get(label, {}).get(table, [])}
                for row in rows[table]:
                    if row[KEYS[table][0]] not in removed:
                        protected.update(object_tokens(row))
        files = {}
        for asset in related:
            key = digest([asset[k] for k in ('storage_backend', 'storage_bucket', 'storage_path')])
            record = {k: asset[k] for k in FILE_FIELDS}
            if key in files and files[key]['asset'] != record:
                # Conflicting metadata for one path is never sufficient authority to remove it.
                protected.update(object_tokens(record))
            files[key] = {'id': key, 'asset': record}
        return [({'id': f['id'], 'state': 'preserved'} if object_tokens(f['asset']) & protected else {**f, 'state': 'pending'})
                for _, f in sorted(files.items())]

    def prepare(self, set_id, targets):
        set_id = str(uuid.UUID(str(set_id)))
        targets = sorted(set(targets) | {SOURCE})
        if SOURCE not in self.banks or not set(targets) <= self.banks.keys():
            raise ValueError('삭제 대상 DB 연결 설정이 필요합니다.')
        with ExitStack() as stack:
            connections = {label: stack.enter_context(connection(url)) for label, url in sorted(self.banks.items())}
            inventories, deleted, related, identities = self._read(connections, set_id, targets)
            if not deleted[SOURCE]['question_sets']:
                raise ValueError('선택한 세트가 생성기 DB에 없습니다. 새로고침하세요.')
            files = self._files(inventories, deleted, related)
            for f in files:
                if f['state'] == 'pending':
                    self.deleter.validate(f['asset'])
            return {'id': uuid.uuid4().hex, 'version': 1, 'config': self.config, 'set_id': set_id,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'prepared',
                    'identities': identities, 'targets': targets, 'files': files,
                    'databases': {label: {'before': digest(rows), 'counts': {t: len(r) for t, r in rows.items()},
                                          'state': 'pending'} for label, rows in deleted.items()}}

    def execute(self, plan):
        with execution_lock(self.root):
            if self._path(plan['id']).exists():
                raise ValueError('이미 시작한 삭제 작업입니다. 남은 삭제 이어서 처리를 이용하세요.')
            return self._run(json.loads(json.dumps(plan)), fresh=True)

    def resume(self, job_id):
        with execution_lock(self.root):
            return self._run(json.loads(self._path(job_id).read_text(encoding='utf-8')), fresh=False)

    def _run(self, job, *, fresh):
        if job['config'] != self.config or job['version'] != 1:
            raise ValueError('공유 확인 대상 DB 설정이 바뀌었습니다. 기존 설정을 확인하세요.')
        try:
            with ExitStack() as stack:
                connections = {label: stack.enter_context(connection(url, readonly=False)) for label, url in sorted(self.banks.items())}
                for c in connections.values():
                    _lock(c)
                inventories, deleted, related, identities = self._read(connections, job['set_id'], job['targets'])
                if identities != job['identities']:
                    raise ValueError('조회한 DB 대상이 변경되었습니다. 다시 확인하세요.')
                for label, state in job['databases'].items():
                    rows = deleted[label]
                    absent = not any(rows.values())
                    if state['state'] == 'done' or (state['state'] == 'started' and absent):
                        if not absent:
                            raise ValueError('삭제한 세트가 다시 등록되었습니다. 이어서 삭제하지 않았습니다.')
                        state['state'] = 'done'
                    elif digest(rows) != state['before']:
                        raise ValueError('확인 후 세트·문항·미디어가 변경되었습니다. 다시 확인하세요.')
                if fresh and self._files(inventories, deleted, related) != job['files']:
                    raise ValueError('파일 공유 상태가 바뀌었습니다. 삭제 내용을 다시 확인하세요.')
                self.deleter.preflight([f['asset'] for f in job['files'] if f['state'] == 'pending'])
                job['status'] = 'running'
                self._save(job)
                for label, state in job['databases'].items():
                    if state['state'] == 'done':
                        continue
                    state['state'] = 'started'
                    self._save(job)
                    c = connections[label]
                    self._delete_rows(c, deleted[label])
                    c.commit()
                    state['state'] = 'done'
                    self._save(job)
                    _lock(c)
                # A commit releases locks: reacquire and recheck every surviving reference.
                protected = set()
                for c in connections.values():
                    for table in ASSETS:
                        for (row,) in c.execute(sql.SQL('SELECT to_jsonb(a) FROM topik_bank.{} a').format(sql.Identifier(table))):
                            protected.update(object_tokens(row))
                for f in job['files']:
                    if f['state'] != 'pending':
                        continue
                    if object_tokens(f['asset']) & protected:
                        f['state'] = 'preserved'
                        f.pop('error', None)
                    else:
                        try:
                            complete = self.deleter.delete(f['asset'], job['id'] + '-' + f['id'])
                            f['state'] = 'deleted' if complete else 'pending'
                            f['error'] = '' if complete else '캐시 제거 완료 확인 대기'
                        except Exception:
                            f['error'] = '파일 삭제 미완료 · 저장소 권한·연결을 확인하고 재시도하세요.'
                    self._save(job)
                job['status'] = 'pending' if any(f['state'] == 'pending' for f in job['files']) else 'complete'
                job.pop('error', None)
                self._save(job)
                return job
        except Exception as exc:
            if self._path(job['id']).exists():
                job['status'] = 'pending'
                job['error'] = str(exc) if isinstance(exc, ValueError) else '삭제 처리가 중단되었습니다. 연결·권한을 확인하고 이어서 처리하세요.'
                self._save(job)
                return job
            raise ValueError(str(exc) if isinstance(exc, ValueError) else '삭제 사전 확인에 실패했습니다. DB·저장소 설정을 확인하세요.') from None
        finally:
            self.deleter.close()

    @staticmethod
    def _delete_rows(c, deleted):
        for table in ('item_audio_bindings', 'item_visual_assets', 'items', 'question_sets', 'tts_audio_assets'):
            ids = [r[KEYS[table][0]] for r in deleted[table]]
            if ids:
                c.execute(sql.SQL('DELETE FROM topik_bank.{} WHERE {}=ANY(%s::uuid[])').format(
                    sql.Identifier(table), sql.Identifier(KEYS[table][0])), (ids,))
