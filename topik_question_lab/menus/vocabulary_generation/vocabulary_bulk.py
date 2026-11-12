"""A bounded in-process runner sharing the project's generation/backup lock."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import threading
import time

from topik_question_lab.common.vocabulary.vocabulary_storage import VocabularyStore
from topik_question_lab.menus.vocabulary_generation.vocabulary_prompts import prompt_config
from topik_question_lab.common.generation.bulk_generation import execution_lock
from topik_question_lab.common.generation.providers import call_provider
from topik_question_lab.menus.vocabulary_generation.vocabulary_generation import ConfigurationError, Paused, process_word
from topik_question_lab.common.vocabulary.vocabulary_questions import VocabularyQuestionBank
from topik_question_lab.common.question_bank.connection import TransferError


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

    def start(self, url, *, snapshots, config):
        with self.guard:
            if self.active:
                raise ValueError("단어 대량 생성이 이미 실행 중입니다.")
            lock = execution_lock(self.root)
            lock.__enter__()
            try:
                bank = self.bank_factory(url)
                identity = bank.identity()
                if not config or not 1 <= config.get("concurrency", 2) <= 3 or not config.get("provider") or not config.get("model"):
                    raise ValueError("모델과 동시 처리 수를 확인하세요.")
                config = {**config, **prompt_config(config)}
                batch_id = self.store.create(identity, config, snapshots)
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

    def _generate_and_save(self, task_id, config, bank, database_key, gate):
        try:
            process_word(self.store, task_id, config, bank, self.provider_call, self.stop_event, gate)
            task = self.store.task(task_id)
            if task["stage"] == "save" and task["status"] == "ready":
                self.store.update(task_id, status="processing")
                try:
                    bank.save(task, config, database_key)
                except Exception:
                    # A lost commit response must not turn a stored word into a new generation.
                    if not bank.confirm_saved(task, config, database_key):
                        raise
                self.store.update(task_id, status="saved", error="")
        except Paused:
            self.store.update(task_id, error="실행 중단 · 다음 생성에서 처음부터 시작합니다.")
        except ConfigurationError:
            raise
        except TransferError:
            self.store.update(task_id, error="DB 연결 또는 저장 확인 실패 · 연결 상태를 확인하고 다시 생성하세요.")
        except ValueError as exc:
            self.store.update(task_id, error=str(exc))
        except Exception:
            self.store.update(task_id, error="단어 처리 실패 · 연결·설정을 확인하고 다시 생성하세요.")
        finally:
            if self.store.task(task_id)["status"] != "saved":
                self.store.reset_for_next_run(task_id)

    def _execute(self, batch_id, bank, lock):
        error = ""
        try:
            batch = self.store.batch(batch_id)
            config = batch["config"]
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
                        future = executor.submit(self._generate_and_save, task["generation_id"], config, bank,
                                                 batch["database_key"], gate)
                        pending[future] = task["generation_id"]
                    if not pending:
                        break
                    done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in done:
                        task_id = pending.pop(future)
                        try:
                            future.result()
                        except ConfigurationError as exc:
                            error = str(exc)
                            self.stop_event.set()
                        except Exception:
                            self.store.reset_for_next_run(task_id, error="단어 처리 실패 · 연결 및 설정을 확인하세요.")
            for task in self.store.tasks(batch_id):
                if task["status"] != "saved":
                    self.store.reset_for_next_run(task["generation_id"])
            if self.stop_event.is_set():
                self.store.set_batch(batch_id, "paused", error)
            else:
                self.store.summarize(batch_id)
        except Exception:
            self.store.set_batch(batch_id, "paused", "실행이 중단되었습니다. 연결·설정을 확인하고 다시 생성하세요.")
        finally:
            lock.__exit__(None, None, None)
