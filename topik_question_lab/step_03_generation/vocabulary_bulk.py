"""A bounded in-process runner sharing the project's generation/backup lock."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
import threading
import time

from topik_question_lab.common.vocabulary_models import digest, validate_bundle
from topik_question_lab.common.vocabulary_storage import VocabularyStore
from topik_question_lab.step_02_planning.vocabulary_prompts import prompt_config, recovery_prompt_config
from topik_question_lab.step_03_generation.bulk_generation import execution_lock
from topik_question_lab.step_03_generation.providers import call_provider
from topik_question_lab.step_03_generation.vocabulary_generation import ConfigurationError, Paused, process_word
from topik_question_lab.step_06_publication.vocabulary_questions import VocabularyQuestionBank
from topik_question_lab.step_06_publication.bank_transfer import TransferError
from topik_question_lab.step_04_review.vocabulary_validation import validation_retry_details


class RateGate:
    def __init__(self):
        self.deadline = 0
        self.lock = threading.Lock()

    def defer(self, deadline):
        with self.lock:
            self.deadline = max(self.deadline, deadline)

    def wait(self, stop, deadline):
        while True:
            with self.lock:
                remaining = max(deadline, self.deadline) - time.time()
            if stop.is_set():
                raise Paused()
            if remaining <= 0:
                return
            if stop.wait(min(remaining, 1)):
                raise Paused()


class VocabularyRunner:
    def __init__(self, root, provider_call=call_provider, bank_factory=VocabularyQuestionBank):
        self.root = Path(root).resolve()
        self.store = VocabularyStore(self.root)
        self.provider_call = provider_call
        self.bank_factory = bank_factory
        self.thread = None
        self.stop_event = threading.Event()
        self.guard = threading.Lock()
        self.active_batch = None
        self.recover_idle()

    @property
    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def recover_idle(self):
        with self.guard:
            if self.active:
                return
            try:
                with execution_lock(self.root):
                    self.store.recover()
            except RuntimeError:
                pass

    @contextmanager
    def exclusive(self):
        with self.guard:
            if self.active:
                raise ValueError("진행 중인 작업을 일시정지한 뒤 실행하세요.")
            with execution_lock(self.root):
                yield

    def start(self, url, *, snapshots=None, config=None, batch_id=None):
        with self.guard:
            if self.active:
                raise ValueError("단어 대량 생성이 이미 실행 중입니다.")
            lock = execution_lock(self.root)
            lock.__enter__()
            try:
                bank = self.bank_factory(url)
                identity = bank.identity()
                if batch_id is None:
                    if not config or not 1 <= config.get("concurrency", 2) <= 3 or not config.get("provider") or not config.get("model"):
                        raise ValueError("모델과 동시 처리 수를 확인하세요.")
                    config = {**config, **prompt_config(config)}
                    batch_id = self.store.create(identity, config, snapshots)
                else:
                    batch = self.store.batch(batch_id)
                    if batch["database_key"] != identity:
                        raise ValueError("배치의 대상 DB와 현재 연결이 다릅니다.")
                    self.store.recover()
                    self.store.retry(batch_id)
                    for task in self.store.tasks(batch_id):
                        details = validation_retry_details(task)
                        if details is None:
                            continue
                        messages, positions = details
                        checkpoint = task["checkpoint"]
                        checkpoint.update(issues=messages, repair_positions=positions, review={},
                                          prompt_override=recovery_prompt_config(batch["config"], checkpoint))
                        checkpoint.pop("pending_response", None)
                        checkpoint.pop("reviewed_hash", None)
                        # Explicit resume authorizes one more targeted repair, not a new generation.
                        self.store.update(task["generation_id"], stage="repair", status="pending", attempts=0,
                                          repair_count=1, checkpoint=checkpoint, error="")
                self.store.set_batch(batch_id, "running")
                self.stop_event.clear()
                self.active_batch = batch_id
                self.thread = threading.Thread(target=self._execute, args=(batch_id, bank, lock), daemon=True,
                                               name="vocabulary-generation")
                self.thread.start()
            except Exception:
                lock.__exit__(None, None, None)
                raise
            return batch_id

    def stop(self):
        self.stop_event.set()

    def _execute(self, batch_id, bank, lock):
        error = ""
        try:
            config = self.store.batch(batch_id)["config"]
            recorded = self.store.tasks(batch_id)
            tasks = iter(task for task in recorded
                         if task["status"] in {"pending", "retry_wait", "failed"} and task["stage"] != "save")
            gate = RateGate()
            gate.defer(max((task["next_attempt_at"] for task in recorded), default=0))
            with ThreadPoolExecutor(max_workers=config.get("concurrency", 2)) as executor:
                pending = {}
                exhausted = False
                while pending or not exhausted:
                    while not exhausted and not self.stop_event.is_set() and len(pending) < config.get("concurrency", 2):
                        task = next(tasks, None)
                        if task is None:
                            exhausted = True
                            break
                        future = executor.submit(process_word, self.store, task["generation_id"], config, bank,
                                                 self.provider_call, self.stop_event, gate)
                        pending[future] = task["generation_id"]
                    if not pending:
                        break
                    done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in done:
                        task_id = pending.pop(future)
                        try:
                            future.result()
                        except Paused:
                            task = self.store.task(task_id)
                            if task["status"] == "processing":
                                self.store.update(task_id, status="pending")
                        except ConfigurationError as exc:
                            error = str(exc)
                            self.stop_event.set()
                        except Exception:
                            self.store.update(task_id, status="failed", error="단어 처리 실패 · 연결 및 설정을 확인하세요.")
            if self.stop_event.is_set():
                self.store.set_batch(batch_id, "paused", error)
            else:
                self.store.summarize(batch_id)
        except Exception:
            self.store.set_batch(batch_id, "paused", "실행이 중단되었습니다. 기록을 확인하고 재개하세요.")
        finally:
            lock.__exit__(None, None, None)

    def edit(self, task_id, payload):
        with self.exclusive():
            task = self.store.task(task_id)
            if task["status"] == "saved":
                raise ValueError("저장된 문항은 새 재생성 작업에서 수정하세요.")
            candidate = validate_bundle(payload, task["snapshot"]["word"]).model_dump()
            checkpoint = task["checkpoint"]
            checkpoint.update(candidate=candidate, review={}, issues=[])
            for key in ("pending_response", "reviewed_hash", "repair_positions"):
                checkpoint.pop(key, None)
            self.store.update(task_id, stage="review", status="pending", attempts=0, repair_count=0,
                              next_attempt_at=0, checkpoint=checkpoint, error="")
            self.store.set_batch(task["batch_id"], "paused")

    def save_ready(self, url, batch_id):
        with self.exclusive():
            bank = self.bank_factory(url)
            batch = self.store.batch(batch_id)
            if bank.identity() != batch["database_key"]:
                raise ValueError("배치의 대상 DB와 현재 연결이 다릅니다.")
            results = []
            for task in self.store.tasks(batch_id):
                if task["stage"] != "save" or task["status"] not in {"ready", "pending", "processing", "failed"}:
                    continue
                try:
                    self.store.update(task["generation_id"], status="processing")
                    bank.save(task, batch["config"], batch["database_key"])
                    self.store.update(task["generation_id"], status="saved", error="")
                    results.append((task["vocabulary_id"], "saved"))
                except TransferError:
                    self.store.update(task["generation_id"], status="failed", error="저장 확인 실패 · 일괄 저장을 다시 실행하세요.")
                    results.append((task["vocabulary_id"], "failed"))
                except ValueError as exc:
                    self.store.update(task["generation_id"], status="needs_attention", error=str(exc))
                    results.append((task["vocabulary_id"], "needs_attention"))
                except Exception:
                    self.store.update(task["generation_id"], status="failed", error="저장 확인 실패 · 일괄 저장을 다시 실행하세요.")
                    results.append((task["vocabulary_id"], "failed"))
            self.store.summarize(batch_id)
            return results
