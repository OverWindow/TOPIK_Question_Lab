"""Start, stop, or inspect the existing local bank server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess

from dotenv import load_dotenv
import psycopg
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[2]
MANAGED = ROOT / ".local" / "postgres-bank"


class BankServerError(ValueError):
    pass


def read_config():
    try:
        config = json.loads((MANAGED / "server.json").read_text(encoding="utf-8"))
        assert config["managed_by"] == "Unigate-PBT-Hub" and config["version"] == 1
        assert config["port"] == 5434
        assert Path(config["data_directory"]).resolve() == (MANAGED / "data").resolve()
        assert (Path(config["bin_directory"]) / "pg_ctl.exe").is_file()
    except (OSError, ValueError, KeyError, AssertionError, TypeError) as exc:
        raise BankServerError("기존 로컬 Bank 서버 설정을 확인하세요.") from exc
    return config


def database_url(config):
    value = os.getenv("DATABASE_URL", "")
    try:
        params = conninfo_to_dict(value)
        loopbacks = {"localhost", "127.0.0.1", "::1"}
        valid = (params.get("host") in loopbacks and params.get("port") == str(config["port"])
                 and params.get("dbname") == "topik_bank" and not params.get("service")
                 and not os.getenv("PGSERVICE")
                 and params.get("hostaddr", "127.0.0.1") in loopbacks
                 and os.getenv("PGHOSTADDR", "127.0.0.1") in loopbacks)
    except psycopg.Error:
        valid = False
    if not valid:
        raise BankServerError("DATABASE_URL은 5434 포트의 로컬 topik_bank DB를 가리켜야 합니다.")
    return value


def server_running(config):
    url = database_url(config)
    try:
        with psycopg.connect(url, connect_timeout=2,
                             options="-c default_transaction_read_only=on -c statement_timeout=5000") as connection:
            actual = Path(connection.execute("SHOW data_directory").fetchone()[0]).resolve()
            if actual != Path(config["data_directory"]).resolve():
                raise BankServerError("접속한 서버가 관리 대상 Bank 인스턴스와 다릅니다.")
            return True
    except psycopg.Error as exc:
        with socket.socket() as probe:
            probe.settimeout(2)
            listening = probe.connect_ex(("127.0.0.1", config["port"])) == 0
        if listening:
            raise BankServerError("Bank 포트는 열려 있지만 접속에 실패했습니다. DATABASE_URL과 서버 상태를 확인하세요.") from exc
        return False


def child_environment():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("AWS_", "CHATKHU_", "DEEPSEEK_", "PG")) or "DATABASE_URL" in key:
            env.pop(key, None)
    scratch = MANAGED / "temp"
    scratch.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
    return env


def control_server(config, action):
    arguments = [str(Path(config["bin_directory"]) / "pg_ctl.exe"), "-D", config["data_directory"]]
    arguments += (["-l", config["log_file"]] if action == "start" else ["-m", "fast"])
    arguments += ["-w", "-t", "60", action]
    # Windows background children must not inherit captured output pipes.
    result = subprocess.run(arguments, env=child_environment(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=75,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise BankServerError("Bank 서버 제어에 실패했습니다. 서버 로그를 확인하세요.")


def main(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status"))
    args = parser.parse_args(argv)
    try:
        config = read_config()
        running = server_running(config)
        if args.command == "start" and not running:
            control_server(config, "start")
            running = server_running(config)
        elif args.command == "stop" and running:
            control_server(config, "stop")
            running = server_running(config)
        print(json.dumps({"running": running, "port": config["port"], "database": "topik_bank"}))
        return 0
    except Exception as exc:
        print(str(exc) if isinstance(exc, BankServerError) else "Bank 서버 상태 확인에 실패했습니다.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
