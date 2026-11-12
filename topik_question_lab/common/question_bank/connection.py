"""Shared database identity, guarded connections, and snapshot hashes."""
from contextlib import contextmanager
import hashlib
import json

import psycopg


class TransferError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


@contextmanager
def connection(url, *, readonly=True):
    if not url.strip():
        raise TransferError("DB 연결 환경변수를 설정하세요.")
    try:
        with psycopg.connect(url, connect_timeout=8, prepare_threshold=None,
                             options="-c timezone=UTC -c statement_timeout=60000 -c lock_timeout=8000"
                             + (" -c default_transaction_read_only=on" if readonly else "")) as c:
            if readonly:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield c
    except psycopg.Error as exc:
        # Driver messages may include a DSN, row contents, or credentials.
        raise TransferError(f"DB 작업에 실패했습니다 (SQLSTATE {exc.sqlstate or '연결 오류'}). "
                            "연결·권한·스키마를 확인하고 등록 결과를 다시 확인하세요.") from None


def database_identity(c):
    identity = c.execute("SELECT current_database(),inet_server_addr()::text,inet_server_port()" ).fetchone()
    cluster = None
    if c.execute("SELECT has_function_privilege(current_user,'pg_control_system()','EXECUTE')").fetchone()[0]:
        cluster = c.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
    return {"cluster": cluster, "database": identity[0], "address": identity[1], "port": identity[2]}


def _distinct_target(c, source_identity):
    identity = database_identity(c)
    same_cluster = identity["cluster"] and identity["cluster"] == source_identity["cluster"]
    loopbacks = {"127.0.0.1", "::1"}
    same_address = (identity["address"] == source_identity["address"] or
                    {identity["address"], source_identity["address"]}.issubset(loopbacks))
    same_endpoint = same_address and identity["port"] == source_identity["port"]
    if identity["database"] == source_identity["database"] and (same_cluster or same_endpoint):
        raise TransferError("대상이 생성기 원본 DB와 같습니다. 원본에는 전송할 수 없습니다.")
    return identity
