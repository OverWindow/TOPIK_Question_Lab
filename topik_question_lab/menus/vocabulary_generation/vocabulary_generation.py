"""Generate/review, then repair/review at most once using durable checkpoints."""
import time

from topik_question_lab.common.vocabulary.vocabulary_models import balance_answer_positions, conjugates, digest, merge_repair, validate_bundle
from topik_question_lab.menus.vocabulary_generation.vocabulary_prompts import build_prompt
from topik_question_lab.menus.vocabulary_generation.vocabulary_validation import apply_review, validation_feedback
from topik_question_lab.common.question_bank.connection import TransferError


class Paused(Exception):
    pass


class ConfigurationError(Exception):
    pass


def process_word(store, task_id, config, bank, provider_call, stop, gate):
    task = store.task(task_id)
    config = {**config, **task["checkpoint"].get("prompt_override", {})}
    word = task["snapshot"]["word"]
    try:
        conjugates(word)
        current = bank.snapshot(task["vocabulary_id"])
        if digest(current["word"]) != task["source_hash"]:
            raise ValueError("원본 단어가 변경되었습니다. 새 작업으로 생성하세요.")
    except TransferError:
        raise
    except ValueError as exc:
        store.update(task_id, status="needs_attention", error=str(exc))
        return
    while True:
        if stop.is_set():
            raise Paused()
        task = store.task(task_id)
        stage, checkpoint = task["stage"], task["checkpoint"]
        if stage == "save" or task["status"] in {"ready", "saved", "skipped", "needs_attention"}:
            return
        if stage == "repair" and not config.get("repair_on_failure"):
            # Keep older workflows on their recorded policy and response contract.
            store.update(task_id, status="needs_attention",
                         error="이전 작업의 수정 단계는 실행하지 않습니다. 새 작업으로 처음부터 생성하세요.")
            return
        response = checkpoint.get("pending_response")
        if response is None:
            if task["attempts"] >= 3:
                store.update(task_id, status="failed", error="모델 요청 재시도 한도 초과")
                return
            gate.wait(stop, task["next_attempt_at"])
            if stop.is_set():
                raise Paused()
            attempts = task["attempts"] + 1
            store.update(task_id, status="processing", attempts=attempts, next_attempt_at=0)
            user = build_prompt(stage, word, checkpoint, config)
            result = provider_call(provider=config["provider"], model=config["model"],
                                   operation="vocabulary_review" if stage == "review" else "generation",
                                   system_prompt=config["system"], user_prompt=user, managed_retries=True,
                                   request_timeout=120)
            response = result.model_dump(mode="json")
            checkpoint.setdefault("calls", []).append({"stage": stage, "system": config["system"], "prompt": user,
                                                      "phase": "final_review" if stage == "review" and task["repair_count"] else stage,
                                                      "prompt_version": config.get("prompt_version", ""), "result": response})
            # Persist a returned response before interpreting it or honoring pause.
            checkpoint["pending_response"] = response
            store.update(task_id, checkpoint=checkpoint)
        if response.get("error"):
            error = response["error"]
            checkpoint.pop("pending_response", None)
            store.update(task_id, checkpoint=checkpoint)
            if any(marker in error for marker in ("HTTP 401", "HTTP 403", "HTTP 400", "HTTP 404", "키가 없습니다", "invalid parameter")):
                store.update(task_id, status="failed", error="모델 인증·설정을 확인하세요.")
                raise ConfigurationError("모델 인증·설정을 확인한 뒤 다시 생성하세요.")
            if response.get("error_retryable"):
                attempts = store.task(task_id)["attempts"]
                deadline = time.time() + max(float(response.get("error_retry_after") or 0), 10 * 2 ** (attempts - 1))
                gate.defer(deadline)
                store.update(task_id, status="retry_wait" if attempts < 3 else "failed",
                             next_attempt_at=deadline, error="일시적 모델 오류 · 재시도 대기")
                if attempts >= 3:
                    return
                continue
            if not response.get("raw_response"):
                store.update(task_id, status="failed", error="모델 요청에 실패했습니다. 설정을 확인하세요.")
                return
        payload = response.get("parsed_json")
        checkpoint.pop("pending_response", None)
        try:
            if stage == "generate":
                checkpoint["candidate"] = payload
                candidate = balance_answer_positions(validate_bundle(payload, word), seed=task_id).model_dump()
                validate_bundle(candidate, word)
                checkpoint.update(candidate=candidate, issues=[])
                store.update(task_id, stage="review", attempts=0, checkpoint=checkpoint, status="pending", error="")
                continue
            if stage == "repair":
                candidate = merge_repair(payload, checkpoint["candidate"], word, checkpoint["repair_positions"])
                checkpoint.update(candidate=candidate, issues=[], failed_positions=[], review={})
                checkpoint.pop("reviewed_hash", None)
                store.update(task_id, stage="review", attempts=0, checkpoint=checkpoint, status="pending", error="")
                continue
            candidate, review, failed = apply_review(payload, checkpoint["candidate"], word)
            checkpoint.pop("review_errors", None)
            checkpoint.update(candidate=candidate, review=review)
            if review["passed"]:
                checkpoint.update(issues=[], failed_positions=[])
                checkpoint["reviewed_hash"] = digest(candidate)
                store.update(task_id, stage="save", status="ready", attempts=0, checkpoint=checkpoint, error="")
                return
            checkpoint.update(issues=review["issues"], failed_positions=failed)
            if config.get("repair_on_failure") and task["repair_count"] == 0:
                checkpoint.update(repair_positions=failed, initial_review=review)
                checkpoint.pop("reviewed_hash", None)
                store.update(task_id, stage="repair", repair_count=1, attempts=0,
                             checkpoint=checkpoint, status="pending", error="")
                continue
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            messages, positions = validation_feedback(exc, payload if stage == "repair" else checkpoint.get("candidate"))
            if stage == "review":
                # A malformed reviewer response is not evidence of a defective question.
                checkpoint["review_errors"] = messages
                store.update(task_id, status="failed", checkpoint=checkpoint,
                             error="검수 응답 형식 오류: " + "; ".join(messages)[:400])
                return
            checkpoint.update(issues=messages, failed_positions=positions)
        checkpoint.pop("reviewed_hash", None)
        label = ("최종 검수 실패: " if task["repair_count"] else "AI 검수 실패: ") if stage == "review" else (
            "수정 응답 형식 오류: " if stage == "repair" else "문항 형식 오류: ")
        store.update(task_id, status="needs_attention", checkpoint=checkpoint,
                     error=label
                     + "; ".join(checkpoint.get("issues", []))[:400])
        return
