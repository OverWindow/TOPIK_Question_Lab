import hashlib
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.backup_service import (
    BackupOptions,
    create_backup,
    inspect_backup,
    restore_backup,
)
from topik_question_lab.topic_bank import SlotTopicPool, TopicStore


ROOT = Path(__file__).resolve().parents[1]


def seed_workspace(root: Path) -> None:
    reading_db = root / "data" / "types" / "grammar.db"
    listening_db = root / "data" / "listening" / "types" / "visual.db"
    topic_db = root / "data" / "topic_bank.db"
    for path, value in ((reading_db, "reading"), (listening_db, "listening"), (topic_db, "topics")):
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
                connection.execute("INSERT INTO sample VALUES (?)", (value,))
    (root / "extracted_text").mkdir()
    (root / "extracted_text" / "reading_questions.txt").write_text("읽기 인식 결과", encoding="utf-8")
    import_dir = root / "data" / "listening" / "imports" / "sample"
    import_dir.mkdir(parents=True)
    (import_dir / "questions.json").write_text('{"questions": []}', encoding="utf-8")
    (import_dir / "manifest.json").write_text('{"exam": "sample"}', encoding="utf-8")
    source_dir = root / "TOPIK-II-Listening-Script"
    source_dir.mkdir()
    (source_dir / "sample.pdf").write_bytes(b"fake-pdf")
    (root / ".env").write_text("SECRET=never-back-up", encoding="utf-8")


def test_backup_round_trip_restores_databases_and_recognition(tmp_path):
    seed_workspace(tmp_path)
    artifact = create_backup(tmp_path, BackupOptions(include_sources=True))
    manifest = inspect_backup(artifact.data)
    paths = {item["path"] for item in manifest["files"]}
    assert "data/types/grammar.db" in paths
    assert "data/listening/types/visual.db" in paths
    assert "data/topic_bank.db" in paths
    assert next(item for item in manifest["files"] if item["path"] == "data/topic_bank.db")["category"] == "topic_database"
    assert "extracted_text/reading_questions.txt" in paths
    assert "TOPIK-II-Listening-Script/sample.pdf" in paths
    assert ".env" not in paths

    with closing(sqlite3.connect(tmp_path / "data" / "types" / "grammar.db")) as connection:
        with connection:
            connection.execute("UPDATE sample SET value='changed'")
    (tmp_path / "extracted_text" / "reading_questions.txt").write_text("changed", encoding="utf-8")
    extra = tmp_path / "extracted_text" / "keep-me.txt"
    extra.write_text("keep", encoding="utf-8")

    restored, safety_path = restore_backup(tmp_path, artifact.data)
    assert len(restored["files"]) == len(manifest["files"])
    assert safety_path.exists()
    with closing(sqlite3.connect(tmp_path / "data" / "types" / "grammar.db")) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "reading"
    assert (tmp_path / "extracted_text" / "reading_questions.txt").read_text(encoding="utf-8") == "읽기 인식 결과"
    assert extra.read_text(encoding="utf-8") == "keep"


def test_backup_can_exclude_sources_and_rendered_pages(tmp_path):
    seed_workspace(tmp_path)
    page = tmp_path / "data" / "listening" / "imports" / "sample" / "pages" / "page-01.png"
    page.parent.mkdir()
    page.write_bytes(b"png")
    artifact = create_backup(tmp_path, BackupOptions(include_sources=False, include_rendered_pages=False))
    paths = {item["path"] for item in artifact.manifest["files"]}
    assert not any(path.startswith("TOPIK-II-") for path in paths)
    assert not any(path.endswith("page-01.png") for path in paths)


def test_backup_restores_slot_pools_and_their_usage_history(tmp_path):
    path = tmp_path / "data" / "topic_bank.db"
    store = TopicStore(path)
    pool = SlotTopicPool(
        id="backup_pool",
        section="listening",
        slot_numbers=[21, 22],
        name="백업 테스트 풀",
        prompt_instruction="선정 항목을 대화로 만듭니다.",
        items=["예약", "문의"],
    )
    store.save_slot_pool(pool)
    brief = store.select_plan(
        section="listening",
        question_type="paired_21_22",
        units=[("set-1", [21, 22])],
    )[0]
    store.record_usages(
        [brief],
        section="listening",
        question_type="paired_21_22",
        provider="test",
        model="test",
        run_id=7,
    )
    artifact = create_backup(tmp_path, BackupOptions(include_sources=False))

    store.save_slot_pool(pool.model_copy(update={"name": "변경됨", "active": False}))
    restore_backup(tmp_path, artifact.data)

    restored_store = TopicStore(path)
    assert restored_store.get_slot_pool("backup_pool").name == "백업 테스트 풀"
    assert restored_store.list_slot_pool_usages()[0]["run_id"] == 7


def test_backup_rejects_path_traversal(tmp_path):
    payload = b"bad"
    manifest = {
        "format": "topik-question-lab-backup",
        "format_version": 1,
        "created_at": "now",
        "files": [
            {
                "path": "../outside.txt",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "category": "other",
            }
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("backup_manifest.json", json.dumps(manifest))
        archive.writestr("../outside.txt", payload)
    with pytest.raises(ValueError, match="허용되지 않은"):
        inspect_backup(output.getvalue())


def test_backup_restore_page_renders():
    app = AppTest.from_file(str(ROOT / "topik_question_lab" / "backup_restore_app.py"), default_timeout=20).run()
    assert not app.exception
    assert any(button.label == "읽기·듣기 전체 백업 생성" for button in app.button)
    assert any(button.label == "검증된 백업 복원" for button in app.button)
