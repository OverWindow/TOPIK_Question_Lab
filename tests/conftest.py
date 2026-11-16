import os
import uuid

import pytest


@pytest.fixture
def writing_png():
    from PIL import Image
    from topik_question_lab.common.writing_models import WritingQuestion
    from topik_question_lab.menus.writing.writing_images import image_path

    def create(question):
        if isinstance(question, dict):
            question = WritingQuestion.model_validate(question)
        path = image_path(question)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (120, 90), "white").save(path, "PNG")
        return path

    return create


@pytest.fixture
def writing_cloud(monkeypatch):
    from topik_question_lab.common.media.media_cloud import digest_file
    from topik_question_lab.menus.writing import writing_images

    class Store:
        bucket = "test-writing"
        base_url = "https://media.example.com"
        uploaded = []
        verified = []
        objects = {}
        failure = None
        closed = 0

        def __init__(self, *args):
            pass

        def upload(self, path, key, size, sha256, mime):
            assert digest_file(path) == (size, sha256)
            if self.failure == "upload":
                raise RuntimeError("secret-provider-error")
            self.objects[key] = path.read_bytes()
            self.uploaded.append(key)

        def verify(self, row, *, local_path):
            if self.failure == "verify":
                raise RuntimeError("secret-provider-error")
            assert self.objects[row["storage_path"]] == local_path.read_bytes()
            self.verified.append(row)

        def close(self):
            type(self).closed += 1

    monkeypatch.setattr(writing_images, "S3MediaStore", Store)
    return Store


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
    for name in ("BIZROUTER_IMAGE_MODEL", "BIZROUTER_BASE_URL", "GOOGLE_IMAGE_MODEL",
                 "GOOGLE_TTS_MODEL", "GOOGLE_TTS_FEMALE_VOICE", "GOOGLE_TTS_MALE_VOICE",
                 "BIZROUTER_TTS_MODEL", "BIZROUTER_TTS_FEMALE_VOICE", "BIZROUTER_TTS_MALE_VOICE"):
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
