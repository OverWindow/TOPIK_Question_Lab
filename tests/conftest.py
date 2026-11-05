import os
import uuid

import pytest


@pytest.fixture(autouse=True)
def isolated_application_root(tmp_path, monkeypatch):
    """App smoke tests must never open the user's databases or credentials."""
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(tmp_path))
    for name in ("DATABASE_URL", "QUESTION_BANK_LOCAL_DATABASE_URL", "QUESTION_BANK_PRODUCTION_DATABASE_URL", "CHATKHU_API_KEY", "DEEPSEEK_API_KEY", "BIZROUTER_API_KEY"):
        monkeypatch.setenv(name, "")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                 "AWS_DEFAULT_PROFILE", "S3_BUCKET", "CLOUDFRONT_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name in ("GOOGLE_CLOUD_PROJECT_ID", "GOOGLE_CLOUD_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    import streamlit as st
    st.cache_resource.clear()
    st.cache_data.clear()


@pytest.fixture(scope="session")
def postgres_database_factory():
    """Create only disposable databases on an explicitly supplied loopback server."""
    admin_url = os.getenv("TEST_PG_ADMIN_URL", "")
    if not admin_url:
        pytest.skip("TEST_PG_ADMIN_URL enables disposable local PostgreSQL tests")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    config = conninfo_to_dict(admin_url)
    if config.get("host") not in {"localhost", "127.0.0.1", "::1"} or config.get("service"):
        pytest.fail("PostgreSQL tests require an explicit loopback host without service overrides")
    if config.get("hostaddr") and config["hostaddr"] not in {"127.0.0.1", "::1"}:
        pytest.fail("PostgreSQL tests require a loopback hostaddr")
    names = []

    def create():
        name = "codex_unversioned_test_" + uuid.uuid4().hex
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        names.append(name)
        return make_conninfo(**{**config, "dbname": name})

    try:
        yield create
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            for name in names:
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
