"""Classify completed generation history using the read-only bank snapshot."""


from topik_question_lab.common.generation.exam_profiles import question_count


ACTIVE_PIPELINE_STATES = {
    "pending", "generating", "tts", "compose", "image", "chart", "upload", "verify", "register", "retry_wait"
}


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
