import json
from pathlib import Path
import subprocess

import psycopg
import pytest

from topik_question_lab.maintenance import bank_server as bank


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(bank, 'ROOT', tmp_path)
    monkeypatch.setattr(bank, 'MANAGED', tmp_path / 'managed')
    for key in ('PGSERVICE', 'PGHOSTADDR'):
        monkeypatch.delenv(key, raising=False)
    data, binaries = bank.MANAGED / 'data', tmp_path / 'bin'
    data.mkdir(parents=True)
    binaries.mkdir()
    (binaries / 'pg_ctl.exe').touch()
    result = {'version': 1, 'managed_by': 'Unigate-PBT-Hub', 'port': 5434,
              'data_directory': str(data), 'bin_directory': str(binaries),
              'log_file': str(bank.MANAGED / 'server.log')}
    (bank.MANAGED / 'server.json').write_text(json.dumps(result))
    monkeypatch.setenv('DATABASE_URL', 'postgresql://bank_admin:test@127.0.0.1:5434/topik_bank')
    return result


def mock_running(config, monkeypatch):
    queries = []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement):
            queries.append(statement)
            assert statement == 'SHOW data_directory'
            return self
        def fetchone(self): return (config['data_directory'],)

    def connect(url, **kwargs):
        assert 'default_transaction_read_only=on' in kwargs['options']
        return Connection()

    monkeypatch.setattr(bank.psycopg, 'connect', connect)
    return queries


def test_status_is_readonly_and_start_does_not_restart_running_server(config, monkeypatch, capsys):
    queries = mock_running(config, monkeypatch)
    monkeypatch.setattr(bank, 'control_server', lambda *args: pytest.fail('running server must not be restarted'))
    assert bank.main(['status']) == 0
    assert json.loads(capsys.readouterr().out)['running'] is True
    assert bank.main(['start']) == 0
    assert json.loads(capsys.readouterr().out)['running'] is True
    assert queries == ['SHOW data_directory', 'SHOW data_directory']


@pytest.mark.parametrize('url', [
    'postgresql://test@localhost:5432/Unigate',
    'postgresql://test@localhost:5434/postgres',
    'postgresql://test@remote.invalid:5434/topik_bank',
    'postgresql://test@localhost:5434/topik_bank?hostaddr=203.0.113.1',
])
def test_other_servers_and_databases_are_rejected(config, monkeypatch, url):
    monkeypatch.setenv('DATABASE_URL', url)
    with pytest.raises(bank.BankServerError):
        bank.database_url(config)


@pytest.mark.parametrize('field,value', [('port', 5432), ('port', 5433), ('data_directory', 'outside')])
def test_other_server_configurations_are_rejected(config, field, value):
    config[field] = value
    (bank.MANAGED / 'server.json').write_text(json.dumps(config))
    with pytest.raises(bank.BankServerError):
        bank.read_config()


def test_auth_failure_on_live_port_does_not_stop_or_start_server(config, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise psycopg.OperationalError('synthetic-secret')

    class Listening:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect_ex(self, endpoint): return 0

    monkeypatch.setattr(bank.psycopg, 'connect', denied)
    monkeypatch.setattr(bank.socket, 'socket', Listening)
    monkeypatch.setattr(bank, 'control_server', lambda *args: pytest.fail('unverified server must not be controlled'))
    assert bank.main(['stop']) == 1
    assert 'synthetic-secret' not in capsys.readouterr().out


def test_child_process_does_not_receive_credentials_or_capture_background_pipes(config, monkeypatch):
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'CHATKHU_API_KEY', 'DEEPSEEK_API_KEY', 'BIZROUTER_API_KEY'):
        monkeypatch.setenv(key, 'synthetic-secret')
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
        assert 'DATABASE_URL' not in kwargs['env']
        assert 'synthetic-secret' not in kwargs['env'].values()
        assert Path(kwargs['env']['TEMP']).is_relative_to(bank.MANAGED)
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(bank.subprocess, 'run', run)
    bank.control_server(config, 'start')
    assert calls[0][-1] == 'start'
