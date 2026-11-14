"""Durable exam batches and a single background runner per project."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections import Counter
from topik_question_lab.common.generation.exam_profiles import profiles_for_exam, exam_for_type, question_count, profile_for
from contextlib import closing, contextmanager
from pathlib import Path

from topik_question_lab.common.generation.generation_service import build_generation_snapshot, record_generation_topics, save_generation
from topik_question_lab.common.listening_models import ListeningQuestionExample
from topik_question_lab.common.generation.listening_profiles import LISTENING_TYPE_PROFILES
from topik_question_lab.common.listening_storage import ListeningStorage
from topik_question_lab.common.models import ProviderResult, QuestionExample
from topik_question_lab.common.generation.prompt_profiles import QUESTION_TYPE_PROFILES
from topik_question_lab.common.generation.providers import DEFAULT_GENERATION_PRESET, DEFAULT_PROVIDERS, GENERATION_PRESETS, BIZROUTER_PREFIX, bizrouter_capabilities, parse_bizrouter_models, provider_backend, provider_model, can_gateway_call, call_provider, supports_generation_presets
from topik_question_lab.common.storage import Storage
from topik_question_lab.common.generation.topic_bank import TopicBrief, TopicStore, generation_units


def profiles_for(section, exam_format="pbt"):
    return profiles_for_exam(section, exam_format)


def storage_for(root: Path, section: str, type_id: str, generation_profile=None):
    profile_for(section, type_id, generation_profile)
    directory = root / "data" / ("types" if section == "reading" else "listening/types")
    cls = Storage if section == "reading" else ListeningStorage
    return cls(directory / f"{type_id}.db")


def is_text_model(model: str) -> bool:
    # Tenant model catalogs include media endpoints that cannot answer chat requests.
    value = model.lower()
    return not any(marker in value for marker in (
        "image", "tts", "stt", "transcri", "video", "music", "elevenlabs", "seedance", "seedream",
        "flux", "ideogram", "kling", "ray-2", "hailuo", "pixverse", "recraft", "vidu", "veo",
        "lyria", "runway", "sdxl", "sfx", "lightricks/ltx", "stability-ai",
    ))


def model_options(root: Path) -> list[tuple[str, str]]:
    known = {config.model: key for key, config in DEFAULT_PROVIDERS.items()}
    options = {(key, config.model) for key, config in DEFAULT_PROVIDERS.items()}
    for section, directory in (("reading", root / "data/types"), ("listening", root / "data/listening/types")):
        for path in directory.glob("*.db"):
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                    settings = dict(connection.execute("SELECT key,value FROM settings"))
                options.update((BIZROUTER_PREFIX + item["id"], item["id"])
                               for item in parse_bizrouter_models(settings.get("bizrouter_models", "[]")))
                for key in ("active_providers", "chatkhu_models", "known_models"):
                    try:
                        values = json.loads(settings.get(key, "[]"))
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(values, list):
                        continue
                    for value in values:
                        if not isinstance(value, str) or not value.strip():
                            continue
                        provider = known.get(value, value)
                        model = settings.get(f"model_{provider}", provider_model(provider)).strip()
                        options.add((provider, model))
            except sqlite3.Error:
                continue
    return sorted((provider, model) for provider, model in options if model and is_text_model(model))


def model_capabilities_for(root: Path, model: str) -> dict | None:
    paths = [root / "data/bulk_generation.db", *sorted((root / "data/types").glob("*.db")),
             *sorted((root / "data/listening/types").glob("*.db"))]
    for path in paths:
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                row = connection.execute("SELECT value FROM settings WHERE key='bizrouter_models'").fetchone()
            capabilities = bizrouter_capabilities(row[0], model) if row else None
            if capabilities:
                return capabilities
        except sqlite3.Error:
            continue
    return None


def custom_model(model: str) -> tuple[str, str]:
    model = model.strip()
    if model.startswith(BIZROUTER_PREFIX):
        actual = provider_model(model)
        if not actual or not is_text_model(actual) or "/" not in actual or actual.startswith("bizrouter/"):
            raise ValueError("BizRouter 카탈로그의 텍스트 모델 ID를 입력하세요.")
        return model, actual
    if not model or not is_text_model(model):
        raise ValueError("문제 생성에 사용할 텍스트 모델 ID를 입력하세요.")
    provider = next((key for key, config in DEFAULT_PROVIDERS.items() if config.model == model), model)
    return provider, model


def prepare_batch(root: Path, section: str, provider: str, model: str, preset: str | None, exam_format: str = "pbt") -> tuple[list[dict], list[str]]:
    profiles = profiles_for(section, exam_format)
    expected_count = question_count(exam_format)
    slots = [slot for profile in profiles.values() for slot in profile.question_numbers]
    errors = []
    if sorted(slots) != list(range(1, expected_count + 1)):
        errors.append(f"유형 구성이 정확히 1~{expected_count}번을 포함하지 않습니다.")
    if not is_text_model(model):
        errors.append("텍스트 모델만 사용할 수 있습니다.")
    if not can_gateway_call(provider):
        errors.append("선택 모델의 API 키가 설정되지 않았습니다.")
    if not supports_generation_presets(provider, model):
        preset = None
    elif preset not in GENERATION_PRESETS:
        preset = DEFAULT_GENERATION_PRESET
    store = TopicStore(root / "data/topic_bank.db")
    reserved, counts, snapshots = set(), Counter(), []
    capabilities = model_capabilities_for(root, model) if provider_backend(provider, model) == "bizrouter" else None
    for type_id, profile in sorted(profiles.items(), key=lambda pair: min(pair[1].question_numbers)):
        try:
            storage = storage_for(root, section, type_id)
            if not storage.list_examples(approved_only=True):
                raise ValueError("승인된 기출 예시가 없습니다.")
            shared = profile.shared_passage if section == "reading" else profile.shared_script
            topics = store.select_plan(
                section=section, question_type=type_id,
                units=generation_units(profile.question_numbers, len(profile.question_numbers), shared),
                forbidden_pairs=reserved, initial_pair_counts=counts,
            )
            reserved.update(brief.pair for brief in topics)
            counts.update(brief.pair for brief in topics)
            snapshot = build_generation_snapshot(storage, section, type_id, provider, model, preset,
                                                 len(profile.question_numbers), topics)
            if provider_backend(provider, model) == "bizrouter":
                snapshot["model_capabilities"] = capabilities
            snapshots.append(snapshot)
        except Exception as exc:
            errors.append(f"{profile.number_range}번 · {profile.label}: {exc}")
    return snapshots, errors


class BulkStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "data/bulk_generation.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches(
                    id TEXT PRIMARY KEY,section TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,
                    preset TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS tasks(
                    batch_id TEXT NOT NULL REFERENCES batches(id),type_id TEXT NOT NULL,snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',count INTEGER NOT NULL DEFAULT 0,
                    review_count INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
                    run_id INTEGER,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(batch_id,type_id));
                CREATE TABLE IF NOT EXISTS media_tasks(
                    batch_id TEXT NOT NULL REFERENCES batches(id),task_key TEXT NOT NULL,kind TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL DEFAULT 0,
                    result_json TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(batch_id,task_key));
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(batches)")}
            # Retain retired review fields for saved batches and backup compatibility.
            for name, declaration in {"exam_format": "TEXT NOT NULL DEFAULT 'pbt'", "media_config": "TEXT NOT NULL DEFAULT '{}'", "pipeline_status": "TEXT NOT NULL DEFAULT ''",
                                      "auto_resume": "INTEGER NOT NULL DEFAULT 0", "next_attempt_at": "REAL NOT NULL DEFAULT 0",
                                      "review_config": "TEXT NOT NULL DEFAULT '{}'", "review_summary": "TEXT NOT NULL DEFAULT '{}'"}.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE batches ADD COLUMN {name} {declaration}")
            if "review_state" not in {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}:
                connection.execute("ALTER TABLE tasks ADD COLUMN review_state TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def settings(self):
        with self.connect() as connection:
            return dict(connection.execute("SELECT key,value FROM settings"))

    def set_setting(self, key, value):
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def create(self, snapshots):
        if not snapshots:
            raise ValueError("생성할 유형이 없습니다.")
        first = snapshots[0]
        exam_format = first.get("exam_format", "pbt")
        expected_count = question_count(exam_format)
        if sorted(slot for item in snapshots for slot in item["slots"]) != list(range(1, expected_count + 1)):
            raise ValueError(f"정확히 1~{expected_count}번을 준비해야 합니다.")
        identity = {(item.get("exam_format", "pbt"), item["section"], item["provider"], item["model"], item["preset"]) for item in snapshots}
        if len(identity) != 1 or len({item["type_id"] for item in snapshots}) != len(snapshots):
            raise ValueError("전체 유형의 영역·모델·프리셋이 동일해야 합니다.")
        profiles = profiles_for(first["section"], exam_format)
        if {item["type_id"] for item in snapshots} != set(profiles):
            raise ValueError("전체 유형을 준비해야 합니다.")
        if any(item["slots"] != list(profiles[item["type_id"]].question_numbers)
               or item["count"] != len(item["slots"]) or not item["examples"] for item in snapshots):
            raise ValueError("유형별 번호·문항 수와 승인 예시를 확인하세요.")
        batch_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute("INSERT INTO batches(id,section,provider,model,preset,exam_format,status) VALUES (?,?,?,?,?,?,'running')",
                               (batch_id, first["section"], first["provider"], first["model"], first["preset"], exam_format))
            connection.executemany("INSERT INTO tasks(batch_id,type_id,snapshot_json) VALUES (?,?,?)",
                                   [(batch_id, item["type_id"], json.dumps(item, ensure_ascii=False)) for item in snapshots])
        return batch_id

    def batches(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM batches ORDER BY created_at DESC,rowid DESC")]

    def tasks(self, batch_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE batch_id=? ORDER BY rowid", (batch_id,)).fetchall()
        return [{**dict(row), "snapshot": json.loads(row["snapshot_json"])} for row in rows]

    def update_task(self, batch_id, type_id, status, *, count=0, review_count=0, run_id=None, error=""):
        with self.connect() as connection:
            connection.execute("""UPDATE tasks SET status=?,count=?,review_count=?,run_id=?,error=?,
                                attempts=attempts+? WHERE batch_id=? AND type_id=?""",
                               (status, count, review_count, run_id, error, int(status == "processing"), batch_id, type_id))

    def set_status(self, batch_id, status, error=""):
        with self.connect() as connection:
            connection.execute("UPDATE batches SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (status, error, batch_id))

    def recover(self):
        with self.connect() as connection:
            connection.execute("UPDATE batches SET status='interrupted' WHERE status='running'")
            connection.execute("UPDATE tasks SET status='pending',error='서버 종료로 중단됨' WHERE status='processing'")

    def configure_media(self, batch_id, config):
        with self.connect() as c:
            c.execute("UPDATE batches SET media_config=?,pipeline_status='pending',auto_resume=1 WHERE id=?",
                      (json.dumps(config, ensure_ascii=False), batch_id))

    def pipeline(self, batch_id, status, error="", **fields):
        allowed = {"auto_resume", "next_attempt_at"}
        if not set(fields) <= allowed:
            raise ValueError("잘못된 작업 필드입니다.")
        with self.connect() as c:
            assignments = "".join(f",{key}=?" for key in fields)
            c.execute(f"UPDATE batches SET pipeline_status=?,error=?,updated_at=CURRENT_TIMESTAMP{assignments} WHERE id=?",
                      (status, error, *fields.values(), batch_id))

    def media_tasks(self, batch_id):
        with self.connect() as c:
            return [dict(row) for row in c.execute("SELECT * FROM media_tasks WHERE batch_id=? AND active ORDER BY rowid", (batch_id,))]

    def media_task(self, batch_id, key, kind, snapshot):
        with self.connect() as c:
            row = c.execute("SELECT * FROM media_tasks WHERE batch_id=? AND task_key=?", (batch_id, key)).fetchone()
            if row is None:
                cached = c.execute("SELECT result_json FROM media_tasks WHERE task_key=? AND status='succeeded' ORDER BY rowid DESC LIMIT 1", (key,)).fetchone()
                c.execute("INSERT INTO media_tasks(batch_id,task_key,kind,snapshot_json,result_json) VALUES (?,?,?,?,?)",
                          (batch_id, key, kind, json.dumps(snapshot, ensure_ascii=False), cached[0] if cached else "{}"))
            c.execute("UPDATE media_tasks SET active=1 WHERE batch_id=? AND task_key=?", (batch_id, key))
            return dict(c.execute("SELECT * FROM media_tasks WHERE batch_id=? AND task_key=?", (batch_id, key)).fetchone())

    def update_media(self, batch_id, key, **fields):
        if not fields or not set(fields) <= {"status", "attempts", "next_attempt_at", "result_json", "error"}:
            raise ValueError("잘못된 미디어 작업 필드입니다.")
        with self.connect() as c:
            c.execute(f"UPDATE media_tasks SET {','.join(f'{name}=?' for name in fields)} WHERE batch_id=? AND task_key=?",
                      (*fields.values(), batch_id, key))

    def reset_media_attempts(self, batch_id):
        with self.connect() as c:
            c.execute("UPDATE media_tasks SET attempts=0,next_attempt_at=0 WHERE batch_id=? AND status!='succeeded'", (batch_id,))


@contextmanager
def execution_lock(root: Path):
    """OS lock is released on process exit, unlike a persisted 'running' flag."""
    path = root / "data/bulk_generation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("다른 일괄 생성이 실행 중입니다. 완료 후 실행하세요.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class BulkRunner:
    def __init__(self, root: Path, provider_call=call_provider):
        self.root = root.resolve()
        self.store = BulkStore(self.root)
        self.provider_call = provider_call
        self.thread = None
        self.stop_event = threading.Event()
        self._guard = threading.Lock()
        self.recover_idle()

    def recover_idle(self):
        """Also recover a restored backup when this runner is already cached."""
        resume_id = None
        with self._guard:
            if self.thread and self.thread.is_alive():
                return
            try:
                with execution_lock(self.root):
                    self.store.recover()
                    resume_id = next((b["id"] for b in reversed(self.store.batches())
                                      if b["auto_resume"] and b["pipeline_status"] not in ("succeeded", "failed", "paused", "ready", "needs_attention")), None)
            except RuntimeError:
                pass
        if resume_id:
            try:
                self.start(batch_id=resume_id, recovering=True)
            except (RuntimeError, ValueError):
                pass

    def stop(self, batch_id):
        self.store.pipeline(batch_id, "paused", auto_resume=0)
        self.stop_event.set()

    def start(self, snapshots=None, batch_id=None, *, media_config=None, recovering=False):
        with self._guard:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("일괄 생성이 실행 중입니다.")
            lock = execution_lock(self.root)
            lock.__enter__()
            try:
                if batch_id:
                    from topik_question_lab.common.local_cleanup import batch_was_cleaned
                    if batch_was_cleaned(self.root, batch_id):
                        raise ValueError("등록 완료 후 로컬 정리한 묶음입니다. PostgreSQL 세트에서 확인하세요.")
                    batch = next((item for item in self.store.batches() if item["id"] == batch_id), None)
                    if batch is None:
                        raise ValueError("생성 묶음을 찾을 수 없습니다.")
                    if (batch["status"] == "succeeded" and not media_config and
                            (not json.loads(batch["media_config"]) or batch["pipeline_status"] == "succeeded")):
                        raise ValueError("재개할 미완료 작업이 없습니다.")
                    self.store.set_status(batch_id, "running")
                else:
                    batch_id = self.store.create(snapshots)
                if media_config:
                    self.store.configure_media(batch_id, media_config)
                current = next(b for b in self.store.batches() if b["id"] == batch_id)
                if json.loads(current["media_config"]):
                    self.store.pipeline(batch_id, "generating", auto_resume=1)
                    if not recovering:
                        self.store.reset_media_attempts(batch_id)
                        with self.store.connect() as c:
                            c.execute("UPDATE tasks SET attempts=0 WHERE batch_id=? AND status!='succeeded'", (batch_id,))
                self.stop_event.clear()
                self.thread = threading.Thread(target=self._execute, args=(batch_id, lock), daemon=True)
                self.thread.start()
            except Exception:
                lock.__exit__(None, None, None)
                raise
            return batch_id

    def _approve_batch(self, batch_id):
        batch = next(b for b in self.store.batches() if b["id"] == batch_id)
        expected_count = question_count(batch.get("exam_format", "pbt"))
        stores, slots = [], []
        for task in self.store.tasks(batch_id):
            snapshot = task["snapshot"]
            storage = storage_for(self.root, snapshot["section"], task["type_id"], snapshot.get("generation_profile"))
            receipt = storage.generation_receipt(batch_id)
            rows = [row for row in storage.list_generated() if row["batch_id"] == batch_id]
            if (not receipt or sorted(receipt["slots"]) != sorted(snapshot["slots"])
                    or {row["id"] for row in rows} != set(receipt["question_ids"])):
                raise ValueError(f"자동 승인에는 삭제되지 않은 1~{expected_count}번 생성 문항이 모두 필요합니다.")
            slots.extend(receipt["slots"])
            stores.append(storage)
        if sorted(slots) != list(range(1, expected_count + 1)):
            raise ValueError(f"자동 승인에는 정확히 1~{expected_count}번 생성 문항이 필요합니다.")
        for storage in stores:
            storage.approve_generation(batch_id)

    def _execute(self, batch_id, lock):
        from topik_question_lab.common.media.media_provider import MediaError, MediaSettings, RequestRunner, Stopped
        config = {}
        try:
            batch = next(b for b in self.store.batches() if b["id"] == batch_id)
            config = json.loads(batch["media_config"])
            existing_batch = json.loads(batch["review_config"]).get("existing_batch", False)
            if config:
                from topik_question_lab.common.media.media_generation import preflight
                current = preflight(self.root, MediaSettings(**config["settings"]),
                    batch["section"], os.getenv("DATABASE_URL", ""), batch.get("exam_format", "pbt"))
                if current["target"] != config["target"]:
                    raise ValueError("실행 당시와 DB 대상이 달라 자동 실행을 중단했습니다.")
            topics_store = TopicStore(self.root / "data/topic_bank.db")
            for task in self.store.tasks(batch_id):
                if self.stop_event.is_set():
                    raise Stopped()
                snapshot, type_id = task["snapshot"], task["type_id"]
                try:
                    storage = storage_for(self.root, snapshot["section"], type_id, snapshot.get("generation_profile"))
                    receipt = storage.generation_receipt(batch_id)
                    if receipt:
                        if sorted(receipt["slots"]) != sorted(snapshot["slots"]):
                            raise ValueError("저장된 문항 번호가 변경되었습니다. 검수 화면에서 확인하세요.")
                        self.store.update_task(batch_id, type_id, "succeeded", count=receipt["count"],
                                               review_count=receipt["review_count"], run_id=receipt["run_id"])
                        # Repair the bookkeeping if the process stopped after the
                        # question commit; TopicStore's receipt makes this idempotent.
                        result = ProviderResult(provider=snapshot["provider"], model=snapshot["model"], operation="generation")
                        try:
                            record_generation_topics(topics_store, [TopicBrief.model_validate(value) for value in snapshot["topics"]],
                                                     snapshot["section"], type_id, result, receipt["run_id"], batch_id)
                        except Exception as exc:
                            self.store.update_task(batch_id, type_id, "succeeded", count=receipt["count"],
                                                   review_count=receipt["review_count"], run_id=receipt["run_id"], error=str(exc))
                        continue
                    if existing_batch:
                        raise ValueError("기존 문항의 생성 영수증이 없어 재생성 없이 중단했습니다.")
                    def invoke():
                        self.store.update_task(batch_id, type_id, "processing")
                        extra = {"managed_retries": True} if config and self.provider_call is call_provider else {}
                        if "model_capabilities" in snapshot:
                            extra["model_capabilities"] = snapshot["model_capabilities"]
                        value = self.provider_call(snapshot["provider"], snapshot["model"], "generation",
                            snapshot["system"], snapshot["user"], generation_preset=snapshot["preset"], **extra)
                        if config and value.error_retryable:
                            raise MediaError("텍스트 생성 요청 실패", retryable=True, retry_after=value.error_retry_after)
                        return value
                    if config:
                        request = RequestRunner(MediaSettings(**config["settings"]), stop=self.stop_event,
                            checkpoint=lambda **fields: self.store.pipeline(batch_id, "generating",
                                next_attempt_at=fields.get("next_attempt_at", 0)))
                        current_batch = next(b for b in self.store.batches() if b["id"] == batch_id)
                        result = request.call("text", invoke, attempts=task["attempts"], retry_at=current_batch["next_attempt_at"])
                    else:
                        result = invoke()
                    example_cls = QuestionExample if snapshot["section"] == "reading" else ListeningQuestionExample
                    outcome = save_generation(
                        storage, result, snapshot["system"], snapshot["user"], section=snapshot["section"],
                        type_id=type_id, expected_count=snapshot["count"], topic_store=topics_store,
                        topics=[TopicBrief.model_validate(value) for value in snapshot["topics"]],
                        strict=True, batch_id=batch_id,
                        examples=[example_cls.model_validate(value) for value in snapshot["examples"]],
                        prompt_mode=snapshot["prompt_mode"], generation_profile=snapshot.get("generation_profile"),
                    )
                    self.store.update_task(batch_id, type_id, "succeeded" if outcome.ok else "failed",
                                           count=outcome.count, review_count=outcome.review_count, run_id=outcome.run_id,
                                           error=outcome.message if not outcome.ok or "실패" in outcome.message else "")
                except Stopped:
                    raise
                except Exception:
                    self.store.update_task(batch_id, type_id, "failed", error="문항 생성 실패 · 설정과 입력을 확인하세요.")
            tasks = self.store.tasks(batch_id)
            count = sum(task["count"] for task in tasks)
            status = "succeeded" if count == question_count(batch.get("exam_format", "pbt")) and all(task["status"] == "succeeded" for task in tasks) else "partial" if count else "failed"
            if status == "succeeded":
                if self.stop_event.is_set():
                    raise Stopped()
                self._approve_batch(batch_id)
            self.store.set_status(batch_id, status)
            if config:
                if status != "succeeded":
                    self.store.pipeline(batch_id, "failed", "문항 생성이 미완료되어 미디어·DB 등록을 보류했습니다.", auto_resume=0, next_attempt_at=0)
                else:
                    from topik_question_lab.common.media.media_generation import MediaPipeline
                    MediaPipeline(self.root, self.store, batch_id, config, stop=self.stop_event).run()
        except Stopped:
            self.store.pipeline(batch_id, "paused", "중단됨 · 완료된 파일은 보존했습니다.", auto_resume=0, next_attempt_at=0)
            if next(b for b in self.store.batches() if b["id"] == batch_id)["status"] == "running":
                self.store.set_status(batch_id, "interrupted")
        except Exception as exc:
            if config:
                message = str(exc) if isinstance(exc, ValueError) else f"미디어 처리 실패 ({type(exc).__name__}) · 작업 기록을 확인하세요."
                self.store.pipeline(batch_id, "failed", message, auto_resume=0, next_attempt_at=0)
                if next(b for b in self.store.batches() if b["id"] == batch_id)["status"] == "running":
                    self.store.set_status(batch_id, "interrupted")
            else:
                self.store.set_status(batch_id, "interrupted", "실행 중 오류가 발생했습니다.")
        finally:
            lock.__exit__(None, None, None)
