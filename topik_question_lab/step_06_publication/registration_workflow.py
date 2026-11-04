"""Frozen registration selections and recoverable manual publication receipts."""
from dataclasses import asdict
from topik_question_lab.step_02_planning.exam_profiles import question_count
from pathlib import Path

from topik_question_lab.step_06_publication.direct_publication import (
    DirectPublicationService, PublicationJobs, PublicationBlocked, _target_fingerprint,
    _hash, expected_item, require_target_schema, make_package, read_batches, check_registered_set,
)
from topik_question_lab.step_06_publication.postgres_storage import PostgresQuestionBank, PublicationStateChanged, item_state, _source_provenance
from topik_question_lab.step_06_publication.publication import (
    PublicationCandidate, PublicationItemMetadata, collect_candidate_catalog,
    build_item_drafts, build_set_draft,
)


def can_register(record):
    return record.eligible and not any(v.get("severity", "error") == "error" for v in record.candidate.validation)


class RegistrationWorkflow:
    def __init__(self, root, url):
        self.root, self.url = Path(root), url
        self.reader = DirectPublicationService(root, url, readonly=True)
        self.target = _target_fingerprint(url)

    def _candidates(self, keys):
        catalog = collect_candidate_catalog(self.root)
        if catalog.read_errors:
            raise PublicationBlocked("문항 기록을 모두 읽지 못했습니다. 저장 현황을 확인하세요.")
        records = {r.candidate.source_key: r for r in catalog.records}
        if not keys or len(set(keys)) != len(keys):
            raise PublicationBlocked("중복 없이 등록할 문제를 선택하세요.")
        for key in keys:
            if key not in records or not can_register(records[key]):
                raise PublicationBlocked(f"{key}: 승인·자동검사 상태를 다시 확인하세요.")
        return [records[key].candidate for key in sorted(keys)]

    def prepare(self, mode, keys, metadata, *, batch_id=None):
        if mode not in {"add", "update", "set", "batch"}:
            raise PublicationBlocked("등록 작업이 올바르지 않습니다.")
        candidates = self._candidates(keys)
        with self.reader.connect(readonly=True) as c:
            require_target_schema(c)
            baseline = item_state(c, keys)
        if mode == "add" and any(baseline.values()):
            raise PublicationBlocked("이미 등록된 문제가 있습니다. 새로고침 후 다시 선택하세요.")
        if mode == "update" and not all(baseline.values()):
            raise PublicationBlocked("등록된 문제만 변경 내용을 반영할 수 있습니다.")
        if mode == "set" and any(r and r["set_id"] for r in baseline.values()):
            raise PublicationBlocked("이미 다른 세트에 사용된 문제가 있습니다.")
        if mode == "set" and any(x.batch_id for x in candidates):
            raise PublicationBlocked("일괄 생성 문제는 생성 묶음 등록에서 처리하세요.")
        package = {"kind": "manual_registration", "batch_id": "manual", "mode": mode,
                   "candidates": [asdict(x) for x in candidates], "metadata": metadata,
                   "baseline": baseline, "target": self.target}
        if mode == "batch":
            batch = next((b for b in read_batches(self.root) if b["id"] == batch_id), None)
            records = [r for r in collect_candidate_catalog(self.root).records if r.candidate.batch_id == batch_id]
            if not batch or {r.candidate.source_key for r in records} != set(keys):
                raise PublicationBlocked("생성 묶음의 전체 문항을 선택하세요.")
            package["batch_package"] = make_package(batch, records, metadata)
            package["batch_id"] = batch_id
        else:
            self._draft(package)
        draft = self._draft(package)
        items = draft.items if mode == "set" else draft
        new_count = sum(row is None for row in baseline.values())
        changed = 0
        for item in items:
            old = baseline[item.candidate.source_key]
            if old:
                desired = expected_item(item, old["review_status"] if mode == "update" else "reviewed")
                desired["source_provenance"] = _source_provenance(item.candidate, item.metadata)
                changed += any(old[k] != v for k, v in desired.items())
        package["summary"] = {"new": new_count, "changed": changed, "unchanged": len(items)-new_count-changed,
                              "sets": int(mode in {"set", "batch"} and not any(r and r["set_id"] for r in baseline.values()))}
        return package

    def _draft(self, package):
        candidates = [PublicationCandidate(**r) for r in package["candidates"]]
        meta = package["metadata"]
        overrides = {k: PublicationItemMetadata(**v) for k, v in meta.get("items", {}).items()}
        args = (candidates, overrides, meta.get("level", 4), meta.get("difficulty", 0.0))
        return build_set_draft(*args) if package["mode"] == "set" else build_item_drafts(*args)

    def _check_source(self, package):
        if package["target"] != self.target:
            raise PublicationBlocked("확인 이후 DB 대상이 변경되었습니다. 다시 선택하세요.")
        keys = [PublicationCandidate(**v).source_key for v in package["candidates"]]
        current = [asdict(v) for v in self._candidates(keys)]
        if _hash(current) != _hash(package["candidates"]):
            raise PublicationBlocked("확인 이후 문제·승인 상태가 변경되었습니다. 다시 선택하세요.")

    def execute(self, package):
        if package["mode"] == "batch":
            self._check_source(package)
            service = DirectPublicationService(self.root, self.url)
            job = service.prepare([package["batch_id"]], package["metadata"])
            if _hash(job["payload"]["packages"][0]) != _hash(package["batch_package"]):
                raise PublicationBlocked("생성 묶음이 변경되었습니다. 다시 확인하세요.")
            return service.publish(job["id"])
        jobs = PublicationJobs(self.root)
        job = jobs.save(self.target, [package])
        return self.retry(job["id"])

    def reconcile(self, job_id):
        jobs = PublicationJobs(self.root)
        job = jobs.get(job_id)
        if job["target"] != self.target:
            raise PublicationBlocked("다른 DB의 작업은 조회만 할 수 있습니다.")
        p = job["payload"]["packages"][0]
        if p.get("kind") != "manual_registration":
            if "media" in p:
                try:
                    with self.reader.connect(readonly=True) as c:
                        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                        status = c.execute("SELECT review_status FROM topik_bank.question_sets WHERE set_id=%s", (p["set_id"],)).fetchone()
                        check = dict(p)
                        if status and status[0] == "reviewed":
                            check["review_status"] = "reviewed"
                        result = check_registered_set(c, check)
                    jobs.update(job_id, "succeeded" if result["status"] == "complete" else "failed", {"sets": [result]})
                except PublicationBlocked:
                    jobs.update(job_id, "conflict", message="등록 기록과 DB 내용이 다릅니다. 내용 비교가 필요합니다.")
                except Exception:
                    jobs.update(job_id, "outcome_unknown", message="DB 연결을 확인한 뒤 결과를 다시 확인하세요.")
                return jobs.get(job_id)
            return DirectPublicationService(self.root, self.url).reconcile(job_id)
        try:
            draft = self._draft(p)
            items = draft.items if p["mode"] == "set" else draft
            with self.reader.connect(readonly=True) as c:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                current = item_state(c, p["baseline"])
                complete = True
                for item in items:
                    key = item.candidate.source_key
                    row = current[key]
                    old = p["baseline"][key]
                    status = old["review_status"] if old and p["mode"] == "update" else "reviewed"
                    wanted = expected_item(item, status)
                    wanted["source_provenance"] = _source_provenance(item.candidate, item.metadata)
                    if not row or any(row[k] != v for k, v in wanted.items()):
                        complete = False
                    if row and p["mode"] != "set" and (row["set_id"], row["position"]) != (
                        (old["set_id"], old["position"]) if old else (None, None)
                    ):
                        complete = False
                set_ids = {r["set_id"] for r in current.values() if r and r["set_id"]}
                if p["mode"] == "set":
                    complete = complete and len(set_ids) == 1 and all(r and r["position"] == i.candidate.slot for i in items for r in [current[i.candidate.source_key]])
                    if complete:
                        sid = next(iter(set_ids))
                        row = c.execute("SELECT section,generator_provider,generator_model,generator_version,review_status,default_target_level,exam_format FROM topik_bank.question_sets WHERE set_id=%s", (sid,)).fetchone()
                        complete = row == (draft.section, draft.backend, draft.provider_key, draft.model_id, "reviewed", draft.default_target_level, draft.exam_format)
                        complete = complete and c.execute("SELECT count(*) FROM topik_bank.items WHERE set_id=%s", (sid,)).fetchone()[0] == question_count(draft.exam_format)
                state = "succeeded" if complete else "failed" if current == p["baseline"] else "conflict"
                result = {"items": len(items), "set_ids": sorted(set_ids), "mode": p["mode"]} if complete else {}
            jobs.update(job_id, state, result, "" if complete else "미반영 상태입니다. 재시도할 수 있습니다." if state == "failed" else "DB 내용이 예상과 다릅니다. 새로고침 후 비교하세요.")
        except Exception:
            jobs.update(job_id, "outcome_unknown", message="등록 결과를 확인하지 못했습니다. 연결 복구 후 결과를 다시 확인하세요.")
        return jobs.get(job_id)

    def retry(self, job_id):
        jobs = PublicationJobs(self.root)
        job = jobs.get(job_id)
        if job["target"] != self.target:
            raise PublicationBlocked("확인 이후 DB 대상이 변경되었습니다.")
        p = job["payload"]["packages"][0]
        if p.get("kind") != "manual_registration":
            if "media" in p:
                raise PublicationBlocked("미디어 작업은 문제 일괄 생성에서 재시도하세요.")
            return DirectPublicationService(self.root, self.url).publish(job_id)
        if job["status"] != "prepared":
            checked = self.reconcile(job_id)
            if checked["status"] != "failed":
                return checked
        try:
            self._check_source(p)
        except ValueError:
            jobs.update(job_id, "stale", message="문제·승인 상태가 변경되었습니다. 다시 선택하세요.")
            return jobs.get(job_id)
        jobs.update(job_id, "publishing")
        try:
            bank, draft = PostgresQuestionBank(self.url), self._draft(p)
            kwargs = {"expected_state": p["baseline"], "validate_source": lambda: self._check_source(p)}
            if p["mode"] == "set":
                bank.publish_set(draft, **kwargs)
            else:
                bank.publish_items(draft, **kwargs)
        except PublicationStateChanged as exc:
            jobs.update(job_id, "conflict", message=str(exc))
            return jobs.get(job_id)
        except Exception:
            jobs.update(job_id, "outcome_unknown", message="등록 결과를 다시 확인합니다.")
        return self.reconcile(job_id)
