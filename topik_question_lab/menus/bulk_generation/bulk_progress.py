"""Classify completed generation history using the read-only bank snapshot."""


from topik_question_lab.common.generation.exam_profiles import question_count
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog


ACTIVE_PIPELINE_STATES = {
    "pending", "generating", "tts", "compose", "image", "chart", "upload", "verify", "register", "retry_wait"
}


def batch_review_details(root, batch_id):
    """Read current reviews instead of the generation-time receipt counts."""
    catalog = collect_candidate_catalog(root)
    counts, questions = {}, []
    approved = 0
    for record in catalog.records:
        candidate = record.candidate
        if candidate.batch_id != batch_id:
            continue
        issues = list(candidate.validation)
        counts.setdefault(candidate.question_type, 0)
        approved += bool(candidate.review.get("approved"))
        if not issues:
            continue
        counts[candidate.question_type] += 1
        # Older saved chart errors also need the current, actionable explanation.
        if candidate.section == "reading" and any(i.get("code") == "visual_material" for i in issues):
            from topik_question_lab.common.models import GeneratedQuestion
            from topik_question_lab.common.generation.validation import validate_question
            try:
                current = validate_question(GeneratedQuestion.model_validate(candidate.question), [])
                material_issues = [i.model_dump() for i in current if i.code == "visual_material"]
                if material_issues:
                    issues = [i for i in issues if i.get("code") != "visual_material"] + material_issues
            except (ValueError, TypeError, KeyError):
                pass  # Keep the saved diagnostics if the current draft cannot be parsed.
        questions.append({"section": candidate.section, "type_id": candidate.question_type,
                          "id": candidate.generated_id, "slot": candidate.slot, "issues": issues})
    questions.sort(key=lambda q: (q["slot"], q["id"]))
    return {"counts": counts, "questions": questions, "approved": approved, "errors": catalog.read_errors}


def needs_progress_refresh(batches, *, runner_active=False):
    return runner_active or any(
        b["status"] == "running" or (b.get("auto_resume") and b.get("pipeline_status") in ACTIVE_PIPELINE_STATES)
        for b in batches
    )


def completed_batches(snapshot):
    bank = snapshot["bank"]
    if snapshot["errors"] or not bank["available"] or not bank["media_available"]:
        return []
    sets = {str(s["set_id"]): s for s in bank["sets"]}
    result = []
    for batch in snapshot["batches"]:
        if batch["status"] in {"pending", "processing", "running"} or batch.get("pipeline_status") in ACTIVE_PIPELINE_STATES:
            continue
        expected_count = question_count(batch.get("exam_format", "pbt"))
        sid = batch["set_id"]
        stored_set = sets.get(sid)
        members = [i for i in bank["items"] if str(i["set_id"]) == sid]
        coverage = batch["coverage"]
        if not stored_set or stored_set["review_status"] == "retired" or len(members) != expected_count:
            continue
        if {i["position"] for i in members} != set(range(1, expected_count + 1)) or any(i["review_status"] == "retired" for i in members):
            continue
        if not coverage or coverage["audio_bound"] != coverage["audio_required"] or coverage["images_ready"] != coverage["images_required"]:
            continue
        rows = [r for r in snapshot["rows"] if r["set_id"] == sid]
        # Changed local drafts still need attention; archived originals are intentional.
        if len(rows) != expected_count or any(r["status"] not in {"내용 일치", "로컬 정리 완료"} for r in rows):
            continue
        result.append(batch)
    return result
