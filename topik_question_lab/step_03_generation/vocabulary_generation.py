"""Checkpointed generation, review and bounded repair of one word."""
import time

from topik_question_lab.common.vocabulary_models import conjugates, digest, validate_bundle
from topik_question_lab.step_02_planning.vocabulary_prompts import build_prompt
from topik_question_lab.step_04_review.vocabulary_validation import apply_review, validation_feedback
from topik_question_lab.step_06_publication.bank_transfer import TransferError


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
                raise ConfigurationError("모델 인증·설정을 확인한 뒤 재개하세요.")
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
            if stage in {"generate", "repair"}:
                if stage == "repair" and checkpoint.get("repair_positions"):
                    replacements = payload["questions"]
                    positions = [q["position"] for q in replacements]
                    if len(positions) != len(set(positions)) or set(positions) != set(checkpoint["repair_positions"]):
                        raise ValueError("수정 요청한 문항만 반환해야 합니다.")
                    by_position = {q["position"]: q for q in replacements}
                    payload = {**checkpoint["candidate"], "questions": [by_position.get(q["position"], q)
                                for q in checkpoint["candidate"]["questions"]]}
                checkpoint["candidate"] = payload
                candidate = validate_bundle(payload, word).model_dump()
                checkpoint.update(candidate=candidate, issues=[])
                store.update(task_id, stage="review", attempts=0, checkpoint=checkpoint, status="pending", error="")
                continue
            candidate, review, failed = apply_review(payload, checkpoint["candidate"], word)
            checkpoint.update(candidate=candidate, review=review)
            if review["passed"]:
                checkpoint["reviewed_hash"] = digest(candidate)
                store.update(task_id, stage="save", status="ready", attempts=0, checkpoint=checkpoint, error="")
                return
            checkpoint.update(issues=review["issues"], repair_positions=failed)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            messages, positions = validation_feedback(exc, checkpoint.get("candidate"))
            if stage == "review":
                # A malformed reviewer response is not evidence of a defective question.
                checkpoint["review_errors"] = messages
                store.update(task_id, status="failed", checkpoint=checkpoint,
                             error="검수 응답 형식 오류: " + "; ".join(messages)[:400])
                return
            checkpoint.update(issues=messages, repair_positions=positions)
        if task["repair_count"] >= 1:
            store.update(task_id, status="needs_attention", checkpoint=checkpoint,
                         error="자동 수정 후 확인 필요: " + "; ".join(checkpoint.get("issues", []))[:400])
            return
        store.update(task_id, stage="repair", status="pending", attempts=0,
                     repair_count=1, checkpoint=checkpoint, error="")
