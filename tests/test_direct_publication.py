import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import uuid

import pytest

from topik_question_lab.step_06_publication.direct_publication import (
    DirectPublicationService, PublicationBlocked, batch_set_id, make_package, package_draft,
)
from topik_question_lab.step_06_publication.publication import collect_candidate_catalog
from topik_question_lab.step_02_planning.prompt_profiles import QUESTION_TYPE_PROFILES
from topik_question_lab.step_02_planning.listening_profiles import LISTENING_TYPE_PROFILES


ROOT = Path(__file__).resolve().parents[1]


def seed_batch(root, section="reading", batch_id=None, approved=True):
    batch_id = batch_id or uuid.uuid4().hex
    data = root / "data"
    data.mkdir(exist_ok=True)
    with sqlite3.connect(data / "bulk_generation.db") as c:
        c.execute("""CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, section TEXT,provider TEXT,
            model TEXT,status TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("INSERT INTO batches(id,section,provider,model,status) VALUES (?,?,?,?,'succeeded')",
                  (batch_id, section, "deepseek_v4_1_flash", "deepseek-flash"))
    profiles = QUESTION_TYPE_PROFILES if section == "reading" else LISTENING_TYPE_PROFILES
    directory = data / ("types" if section == "reading" else "listening/types")
    directory.mkdir(parents=True, exist_ok=True)
    for type_id, profile in profiles.items():
        with sqlite3.connect(directory / f"{type_id}.db") as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, batch_id TEXT,
                prompt_system TEXT,prompt_user TEXT,result_json TEXT);
                CREATE TABLE IF NOT EXISTS generated_questions(id INTEGER PRIMARY KEY,run_id INTEGER,
                provider TEXT,model TEXT,data_json TEXT,edited_json TEXT,validation_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS reviews(generated_question_id INTEGER PRIMARY KEY,data_json TEXT);""")
            run = c.execute("INSERT INTO runs(batch_id,prompt_system,prompt_user,result_json) VALUES (?,'system','user','{}')", (batch_id,)).lastrowid
            for slot in profile.question_numbers:
                question = {"type_slot": slot, "question_type": type_id, "answer": 1,
                            "explanation": "정답 해설", "choices": ["가", "나", "다", "라"], "set_id": type_id}
                if section == "reading":
                    question.update(stem="밑줄표현 질문" + ("" if type_id == "similar_expression" else " ( )"),
                        highlight_text="밑줄표현", passage="자료 ( ) (가) (나) (다) (라) (①) (②) (③) (④)",
                        question_prompt="질문", auxiliary_text="주어진 문장", target_grammar="독해")
                else:
                    question.update(dialogue_turns=[{"speaker": "여자", "text": "대본 내용"}],
                        question_prompt="들은 내용은?", target_skill="듣기", repeat_count=profile.repeat_count,
                        visual_kind=profile.visual_kind)
                    if profile.visual_kind != "none":
                        question["choices"] = []
                        question["visual_options"] = [{"number": n, "description": f"장면 {n}", "image_prompt": f"그림 {n}"} for n in range(1, 5)]
                qid = c.execute("""INSERT INTO generated_questions(run_id,provider,model,data_json,validation_json)
                    VALUES (?,'deepseek_v4_1_flash','deepseek-flash',?,'[]')""", (run, json.dumps(question))).lastrowid
                c.execute("INSERT INTO reviews VALUES (?,?)", (qid, json.dumps({"approved": approved})))
    return batch_id


def change_question(root, type_id="grammar_blank", edit=lambda q: q, batch_id=None):
    with sqlite3.connect(root / f"data/types/{type_id}.db") as c:
        row = c.execute("SELECT g.id,g.data_json FROM generated_questions g JOIN runs r ON r.id=g.run_id WHERE (? IS NULL OR r.batch_id=?) ORDER BY g.id LIMIT 1", (batch_id, batch_id)).fetchone()
        q = edit(json.loads(row[1]))
        c.execute("UPDATE generated_questions SET edited_json=? WHERE id=?", (json.dumps(q), row[0]))


def local_package(service, batch_id):
    from topik_question_lab.step_06_publication.direct_publication import read_batches
    batch = next(b for b in read_batches(service.root) if b["id"] == batch_id)
    records = [r for r in service.catalog().records if r.candidate.batch_id == batch_id]
    return make_package(batch, records)


def test_batches_are_separate_and_visuals_convert(tmp_path):
    a = seed_batch(tmp_path)
    b = seed_batch(tmp_path)
    listening = seed_batch(tmp_path, "listening")
    service = DirectPublicationService(tmp_path, "")
    assert [b["count"] for b in service.list_batches()] == [50, 50, 50]
    assert all(not b["reason"] for b in service.list_batches())
    packages = [local_package(service, x) for x in (a, b, listening)]
    assert len({p["set_id"] for p in packages}) == 3
    draft = package_draft(packages[2])
    assert all(len(i.choices) == 4 for i in draft.items)
    assert draft.items[0].candidate.question["visual_options"]


@pytest.mark.parametrize("kind", ["unapproved", "missing", "duplicate", "invalid", "shared", "corrupt"])
def test_invalid_batches_are_visible_and_blocked(tmp_path, kind):
    bid = seed_batch(tmp_path, approved=kind != "unapproved")
    service = DirectPublicationService(tmp_path, "")
    if kind == "corrupt":
        (tmp_path / "data/types/grammar_blank.db").write_bytes(b"broken")
        with pytest.raises(PublicationBlocked, match="DB 읽기 실패"):
            service.list_batches()
        return
    if kind == "missing":
        with sqlite3.connect(tmp_path / "data/types/grammar_blank.db") as c:
            c.execute("DELETE FROM generated_questions WHERE id=1")
    if kind == "duplicate":
        change_question(tmp_path, edit=lambda q: {**q, "type_slot": 2})
    if kind == "invalid":
        change_question(tmp_path, edit=lambda q: {**q, "answer": 9})
    if kind == "shared":
        change_question(tmp_path, "paired_19_20", lambda q: {**q, "passage": "다른 지문 ( )"})
    assert service.list_batches()[0]["reason"]
    with pytest.raises(ValueError):
        service.prepare([bid])


def test_fixed_plan_blocks_edits_approval_changes_target_and_tampering(tmp_path):
    bid = seed_batch(tmp_path)
    service = DirectPublicationService(tmp_path, "postgresql://u@localhost/a")
    job = service.jobs.save(service.target, [local_package(service, bid)])
    other = DirectPublicationService(tmp_path, "postgresql://u@localhost/b")
    with pytest.raises(PublicationBlocked, match="대상"):
        other.publish(job["id"])
    change_question(tmp_path, edit=lambda q: {**q, "explanation": "새 해설"})
    with pytest.raises(PublicationBlocked, match="변경"):
        service.publish(job["id"])
    with service.jobs.connect() as c:
        c.execute("UPDATE jobs SET payload='{}'")
    with pytest.raises(PublicationBlocked, match="해시"):
        service.jobs.get(job["id"])


def test_unapproval_invalidates_prepared_job(tmp_path):
    bid = seed_batch(tmp_path)
    service = DirectPublicationService(tmp_path, "")
    job = service.jobs.save(service.target, [local_package(service, bid)])
    with sqlite3.connect(tmp_path / "data/types/grammar_blank.db") as c:
        c.execute("UPDATE reviews SET data_json='{}'")
    with pytest.raises(PublicationBlocked, match="승인"):
        service.publish(job["id"])


def test_keyword_connection_targets_and_password_redaction():
    from topik_question_lab.step_06_publication.direct_publication import _target_fingerprint, _safe_error
    a = "host=localhost dbname=first user=test password=secret-example"
    b = "host=localhost dbname=second user=test password=secret-example"
    assert _target_fingerprint(a) != _target_fingerprint(b)
    assert "secret-example" not in _safe_error(ValueError("secret-example"), a)


def test_publication_jobs_are_in_backup(tmp_path):
    from topik_question_lab.maintenance.backup_service import create_backup, BackupOptions, restore_backup
    import io
    import zipfile
    bid = seed_batch(tmp_path)
    service = DirectPublicationService(tmp_path, "")
    service.jobs.save(service.target, [local_package(service, bid)])
    # This minimal test fixture does not implement the bulk runner's tables.
    (tmp_path / "data/bulk_generation.db").unlink()
    backup = create_backup(tmp_path, BackupOptions(include_sources=False))
    with zipfile.ZipFile(io.BytesIO(backup.data)) as z:
        assert "data/publication_jobs.db" in z.namelist()
    destination = tmp_path / "restored"
    restore_backup(destination, backup.data)
    restored = DirectPublicationService(destination, "")
    assert restored.jobs.list()[0]["payload"] == service.jobs.list()[0]["payload"]


def test_ui_requires_local_postgres_url(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    bid = seed_batch(tmp_path)
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    app = AppTest.from_file(str(ROOT / "topik_question_lab/ui/postgres_publish_app.py"), default_timeout=20).run()
    assert not app.exception
    assert app.title[0].value == "문제 등록·세트 만들기"
    assert any("로컬 PostgreSQL 연결이 필요합니다" in i.value for i in app.info)
    next(b for b in app.button if b.label == "생성 묶음 등록·승인 보기").click().run()
    assert not app.exception
    next(s for s in app.selectbox if s.label == "생성 묶음").set_value(bid).run()
    assert not app.exception
    assert next(b for b in app.button if b.label == "생성 묶음 내용 확인").disabled


def test_former_database_jobs_are_visible_but_readonly(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    bid = seed_batch(tmp_path)
    former = DirectPublicationService(tmp_path, "postgresql://test@former.invalid/old")
    job = former.jobs.save(former.target, [local_package(former, bid)])
    former.jobs.update(job['id'], 'failed', message='synthetic prior failure')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://test@localhost/new')
    app = AppTest.from_file(str(ROOT / 'topik_question_lab/ui/postgres_publish_app.py'), default_timeout=20).run()
    assert not app.exception
    assert any('이전 DB · 조회 전용' in item.label for item in app.expander)
    assert next(b for b in app.button if b.label == '등록 결과 다시 확인').disabled
    assert not any(b.label == '같은 작업 재시도' for b in app.button)


def test_local_validation_survives_empty_sqlite_catalog_without_legacy_history(local_service, monkeypatch):
    from streamlit.testing.v1 import AppTest
    service, ids = local_service
    monkeypatch.setenv('DATABASE_URL', service.target_url)
    # The PostgreSQL bank can be used without any SQLite candidates.
    monkeypatch.setenv('TOPIK_LAB_ROOT', str(service.root / 'empty'))
    path = str(ROOT / 'topik_question_lab/ui/postgres_publish_app.py')
    app = AppTest.from_file(path, default_timeout=30).run()
    next(b for b in app.button if b.label == '로컬 DB 연결·구조 확인').click().run()
    assert not app.exception
    assert any('연결·구조 확인 완료' in message.value for message in app.success)
    assert not any(h.value == '과거 배포 이력' for h in app.subheader)
    assert not any(b.label == '보관된 배포 이력 조회' for b in app.button)


def test_s3_validation_requires_click_and_uses_selected_sets(local_service, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import topik_question_lab.step_06_publication.media_cloud as cloud
    service, ids = local_service
    published = service.publish(service.prepare(ids)['id'])
    set_id = published['result']['sets'][0]['set_id']
    monkeypatch.setenv('DATABASE_URL', service.target_url)
    monkeypatch.setenv('TOPIK_LAB_ROOT', str(service.root / 'empty'))
    calls = []

    def validate(url, selected):
        calls.append((url == service.target_url, selected))
        return {'sets': 1, 'items': 50, 'files': 1, 'manifest_hash': 'a' * 64}

    monkeypatch.setattr(cloud, 'preview_s3_media', validate)
    app = AppTest.from_file(str(ROOT / 'topik_question_lab/ui/postgres_publish_app.py'), default_timeout=30).run()
    app.session_state['registration-mode'] = 'sets'
    app.run()
    assert not app.exception and not calls
    sets = app.session_state['registration-snapshot']['snapshot']['bank']['sets']
    chosen = next(i for i, s in enumerate(sets) if str(s['set_id']) == set_id)
    next(s for s in app.selectbox if s.label == '확인할 세트').set_value(chosen).run()
    next(b for b in app.button if b.label == '음원·이미지 실제 파일 확인').click().run()
    assert not app.exception
    assert calls == [(True, [set_id])]


@pytest.fixture(scope="module")
def target_database(postgres_database_factory):
    from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    return url


@pytest.fixture
def local_service(tmp_path, target_database):
    import psycopg
    with psycopg.connect(target_database) as c:
        c.execute("TRUNCATE topik_bank.items,topik_bank.question_sets CASCADE")
    ids = [seed_batch(tmp_path), seed_batch(tmp_path, "listening")]
    return DirectPublicationService(tmp_path, target_database), ids


def test_local_two_sets_retry_and_content_change(local_service):
    service, ids = local_service
    job = service.prepare(ids)
    result = service.publish(job["id"])
    assert result["status"] == "succeeded"
    assert [r["verified_items"] for r in result["result"]["sets"]] == [50, 50]
    assert service.publish(job["id"])["status"] == "succeeded"
    with service.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 100
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 2
    change_question(service.root, edit=lambda q: {**q, "explanation": "수정"})
    assert next(b for b in service.list_batches() if b["section"] == "reading")["publication_status"] == "등록 후 변경됨"
    with pytest.raises(PublicationBlocked, match="충돌"):
        service.prepare(ids)


def test_local_transaction_rolls_back_all_sets(local_service, monkeypatch):
    service, ids = local_service
    job = service.prepare(ids)
    original = service._insert
    count = 0
    def fail_second(c, p):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("simulated failure")
        return original(c, p)
    monkeypatch.setattr(service, "_insert", fail_second)
    with pytest.raises(PublicationBlocked, match="simulated"):
        service.publish(job["id"])
    with service.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 0


def test_local_concurrent_same_job_is_idempotent(local_service):
    service, ids = local_service
    job = service.prepare(ids)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: service.publish(job["id"]), range(2)))
    assert all(r["status"] == "succeeded" for r in results)
    with service.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 2


def test_local_commit_disconnect_recovers(local_service, monkeypatch):
    service, ids = local_service
    job = service.prepare(ids)
    original = service.connect
    @contextmanager
    def disconnect(readonly=False):
        with original(readonly) as c:
            yield c
        if not readonly:
            raise OSError("connection lost after commit")
    monkeypatch.setattr(service, "connect", disconnect)
    assert service.publish(job["id"])["status"] == "succeeded"


def test_local_unknown_outcome_resumes_after_restart(local_service, monkeypatch):
    service, ids = local_service
    job = service.prepare(ids)
    original = service.connect
    committed = False
    @contextmanager
    def unavailable(readonly=False):
        nonlocal committed
        if readonly and committed:
            raise OSError("network unavailable")
        with original(readonly) as c:
            yield c
        if not readonly:
            committed = True
    monkeypatch.setattr(service, "connect", unavailable)
    assert service.publish(job["id"])["status"] == "outcome_unknown"
    restarted = DirectPublicationService(service.root, service.target_url)
    assert restarted.publish(job["id"])["status"] == "succeeded"


def test_local_local_receipt_write_failure_recovers(local_service, monkeypatch):
    service, ids = local_service
    job = service.prepare(ids)
    original = service.jobs.update
    def fail_receipt(plan, status, *args, **kwargs):
        if status != "publishing":
            raise OSError("disk full")
        return original(plan, status, *args, **kwargs)
    monkeypatch.setattr(service.jobs, "update", fail_receipt)
    with pytest.raises(OSError):
        service.publish(job["id"])
    restarted = DirectPublicationService(service.root, service.target_url)
    assert restarted.publish(job["id"])["status"] == "succeeded"


def test_local_conflict_after_preview_is_blocked(local_service):
    service, ids = local_service
    job = service.prepare(ids)
    other = service.prepare([ids[0]])
    assert service.publish(other["id"])["status"] == "succeeded"
    with service.connect() as c:
        c.execute("UPDATE topik_bank.items SET explanation='remote edit'")
    with pytest.raises(PublicationBlocked, match="충돌"):
        service.publish(job["id"])
    with service.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 1


def test_local_existing_membership_checks_positions_not_only_sources(local_service):
    service, ids = local_service
    job = service.prepare(ids)
    service.publish(job["id"])
    with service.connect() as c:
        c.execute("UPDATE topik_bank.items SET set_id=NULL,position=NULL WHERE position=50")
    result = service.reconcile(job["id"])
    assert result["status"] == "conflict"


def test_ui_registers_and_recovers_receipt_on_restart(local_service, monkeypatch):
    from streamlit.testing.v1 import AppTest
    service, ids = local_service
    monkeypatch.setenv("TOPIK_LAB_ROOT", str(service.root))
    monkeypatch.setenv("DATABASE_URL", service.target_url)
    path = str(ROOT / "topik_question_lab/ui/postgres_publish_app.py")
    app = AppTest.from_file(path, default_timeout=30).run()
    app.session_state["publication-batch-link"] = ids[0]
    app.run()
    assert next(s for s in app.selectbox if s.label == "생성 묶음").value == ids[0]
    for bid in ids:
        next(s for s in app.selectbox if s.label == "생성 묶음").set_value(bid).run()
        next(b for b in app.button if b.label == "생성 묶음 내용 확인").click().run()
        assert not app.exception
        next(b for b in app.button if b.label.endswith("50문제 세트 등록하기")).click().run()
        assert not app.exception
    assert service.jobs.list()[0]["status"] == "succeeded"
    restarted = AppTest.from_file(path, default_timeout=30).run()
    assert not restarted.exception
    assert any("등록 완료" in e.label for e in restarted.expander)


def test_local_schema_preflight_is_readonly_and_missing_constraint_blocks(local_service):
    service, ids = local_service
    with service.connect(True) as c:
        before = c.execute("SELECT count(*) FROM topik_bank.schema_migrations").fetchone()[0]
    service.prepare(ids)
    with service.connect(True) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.schema_migrations").fetchone()[0] == before
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 0
    with service.connect() as c:
        c.execute("ALTER TABLE topik_bank.question_sets DROP CONSTRAINT question_sets_exam_sequence_key")
    try:
        with pytest.raises(PublicationBlocked, match="필수 제약"):
            service.prepare(ids)
    finally:
        with service.connect() as c:
            c.execute("ALTER TABLE topik_bank.question_sets ADD CONSTRAINT question_sets_exam_sequence_key UNIQUE(exam_format,section,generator_provider,generator_model,generator_version,set_sequence)")
