"""Read-only compatibility for approvals saved before automatic review was removed."""
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

VERSION = "topik-review-v1.1"
MODEL = "gpt-6-luna"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_file(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("검수 파일 경로가 저장소 밖입니다.")
    return path


def files_valid(root, files):
    try:
        return all(hashlib.sha256(safe_file(root, f["path"]).read_bytes()).hexdigest() == f["sha256"] for f in files)
    except (OSError, ValueError, KeyError):
        return False


def summaries(root):
    path = root / "data/bulk_generation.db"
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
        if "review_summary" not in {r[1] for r in c.execute("PRAGMA table_info(batches)")}:
            return {}
        return {r[0]: (json.loads(r[1]), json.loads(r[2])) for r in c.execute(
            "SELECT id,review_config,review_summary FROM batches WHERE review_config!='{}'")}


def summary_valid(root, records, config, summary):
    if summary.get("status") != "passed" or config.get("version") != VERSION or config.get("model") != MODEL:
        return False
    if summary.get("config_hash") != digest(config) or not summary.get("set_passed") or not summary.get("media_complete"):
        return False
    if len(records) != 50 or sorted(r.candidate.slot for r in records) != list(range(1, 51)):
        return False
    hashes = {r.candidate.source_key: digest(r.candidate.question) for r in records}
    if hashes != summary.get("question_hashes") or not files_valid(root, summary.get("files", [])):
        return False
    return not any(r.trashed_at or (r.candidate.review and not r.candidate.review.get("approved")) or
                   any(v.get("severity", "error") == "error" for v in r.candidate.validation) for r in records)


def apply_approvals(root, catalog):
    from topik_question_lab.common.question_bank.publication import CandidateExclusion
    for batch_id, (config, summary) in summaries(root).items():
        records = [r for r in catalog.records if r.candidate.batch_id == batch_id]
        valid = summary_valid(root, records, config, summary)
        keys = {r.candidate.source_key for r in records}
        updated = []
        for r in catalog.records:
            if r.candidate.source_key not in keys:
                updated.append(r)
                continue
            if valid and not r.candidate.review:
                review = {**r.candidate.review, "approved": True, "approval_source": "ai", "model": MODEL,
                          "version": VERSION, "content_hash": summary["question_hashes"][r.candidate.source_key]}
                updated.append(replace(r, candidate=replace(r.candidate, review=review), eligible=True, reason=""))
            else:
                updated.append(r)
        catalog.records = updated
        catalog.exclusions = [x for x in catalog.exclusions if f"{x.section}:{x.question_type}:{x.generated_id}" not in keys]
        catalog.exclusions.extend(CandidateExclusion(r.candidate.section, r.candidate.question_type,
                                 r.candidate.generated_id, r.reason) for r in updated if r.candidate.source_key in keys and not r.eligible)
    catalog.candidates = [r.candidate for r in catalog.records if r.eligible]
