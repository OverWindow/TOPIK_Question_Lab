from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from .listening_profiles import LISTENING_TYPE_PROFILES
from .prompt_profiles import QUESTION_TYPE_PROFILES
from .providers import provider_backend
from .validation import discard_resolved_validation_issues, normalize_question_blank_markers


Section = Literal["reading", "listening", "writing"]
REVIEW_STATUSES = ("reviewed", "pilot", "active", "retired")
PUBLICATION_NAMESPACE = uuid.UUID("3a970e17-7509-4bff-a776-b278d3f07ffd")


@dataclass(frozen=True)
class PublicationCandidate:
    section: Section
    question_type: str
    generated_id: int
    run_id: int
    provider_key: str
    model_id: str
    backend: str
    question: dict
    review: dict
    validation: list[dict]
    prompt_system: str
    prompt_user: str
    created_at: str
    source_db: str

    @property
    def slot(self) -> int:
        return int(self.question.get("type_slot", 0))

    @property
    def source_key(self) -> str:
        return f"{self.section}:{self.question_type}:{self.generated_id}"

    @property
    def item_id(self) -> uuid.UUID:
        return uuid.uuid5(PUBLICATION_NAMESPACE, f"item:{self.source_key}")

    @property
    def prompt_version(self) -> str:
        return _sha256({"system": self.prompt_system, "user": self.prompt_user})

    @property
    def label(self) -> str:
        preview = display_stem(self)
        if len(preview) > 70:
            preview = preview[:67] + "..."
        return f"#{self.generated_id} · run {self.run_id} · {self.question_type} · {preview}"


@dataclass(frozen=True)
class CandidateExclusion:
    section: Section
    question_type: str
    generated_id: int
    reason: str


@dataclass(frozen=True)
class LocalPublicationRecord:
    candidate: PublicationCandidate
    eligible: bool
    reason: str = ""


@dataclass
class CandidateCatalog:
    candidates: list[PublicationCandidate] = field(default_factory=list)
    exclusions: list[CandidateExclusion] = field(default_factory=list)
    records: list[LocalPublicationRecord] = field(default_factory=list)

    def identities(self) -> list[tuple[Section, str, str]]:
        return sorted({(value.section, value.provider_key, value.model_id) for value in self.candidates})

    def for_identity(self, section: Section, provider_key: str, model_id: str) -> list[PublicationCandidate]:
        return [
            value
            for value in self.candidates
            if (value.section, value.provider_key, value.model_id) == (section, provider_key, model_id)
        ]


@dataclass(frozen=True)
class PublicationItemMetadata:
    target_level: int
    predicted_difficulty: float
    primary_skill: str

    def validate(self) -> None:
        if not 1 <= self.target_level <= 6:
            raise ValueError("target_level은 1~6이어야 합니다.")
        if not -3.0 <= self.predicted_difficulty <= 3.0:
            raise ValueError("predicted_difficulty는 -3.0~3.0이어야 합니다.")
        if not self.primary_skill.strip():
            raise ValueError("primary_skill은 비워 둘 수 없습니다.")


@dataclass(frozen=True)
class PublicationItemDraft:
    candidate: PublicationCandidate
    metadata: PublicationItemMetadata
    stem: str
    choices: list[str]
    stem_length: int
    choice_count: int
    content_hash: str


@dataclass(frozen=True)
class PublicationSetDraft:
    section: Section
    provider_key: str
    model_id: str
    backend: str
    default_target_level: int
    default_predicted_difficulty: float
    items: tuple[PublicationItemDraft, ...]

    @property
    def set_id(self) -> uuid.UUID:
        identity = f"set:{self.section}:{self.backend}:{self.provider_key}:{self.model_id}"
        return uuid.uuid5(PUBLICATION_NAMESPACE, identity)


def collect_candidate_catalog(root: Path) -> CandidateCatalog:
    catalog = CandidateCatalog()
    locations: tuple[tuple[Section, Path], ...] = (
        ("reading", root / "data" / "types"),
        ("listening", root / "data" / "listening" / "types"),
    )
    for section, directory in locations:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.db")):
            _collect_database(path, root, section, catalog)
    catalog.candidates.sort(key=lambda value: (value.section, value.provider_key, value.model_id, value.slot, value.created_at, value.generated_id))
    return catalog


def group_by_slot(candidates: Iterable[PublicationCandidate]) -> dict[int, list[PublicationCandidate]]:
    grouped: dict[int, list[PublicationCandidate]] = {slot: [] for slot in range(1, 51)}
    for candidate in candidates:
        if 1 <= candidate.slot <= 50:
            grouped[candidate.slot].append(candidate)
    for values in grouped.values():
        values.sort(key=lambda value: (value.created_at, value.generated_id), reverse=True)
    return grouped


def validate_complete_selection(candidates: Iterable[PublicationCandidate]) -> tuple[PublicationCandidate, ...]:
    values = tuple(sorted(candidates, key=lambda value: value.slot))
    slots = [value.slot for value in values]
    expected = list(range(1, 51))
    if slots != expected:
        missing = [slot for slot in expected if slot not in slots]
        duplicates = sorted({slot for slot in slots if slots.count(slot) > 1})
        details = []
        if missing:
            details.append(f"누락: {missing}")
        if duplicates:
            details.append(f"중복: {duplicates}")
        raise ValueError("1~50번이 각각 한 문항이어야 합니다. " + ", ".join(details))
    identity = {(value.section, value.provider_key, value.model_id) for value in values}
    if len(identity) != 1:
        raise ValueError("한 세트에는 동일한 영역과 모델의 문항만 포함할 수 있습니다.")
    return values


def build_set_draft(
    candidates: Iterable[PublicationCandidate],
    metadata_by_source: dict[str, PublicationItemMetadata],
    default_target_level: int,
    default_predicted_difficulty: float,
) -> PublicationSetDraft:
    selected = validate_complete_selection(candidates)
    if not 1 <= default_target_level <= 6:
        raise ValueError("세트 기본 TOPIK 급수는 1~6이어야 합니다.")
    if not -3.0 <= default_predicted_difficulty <= 3.0:
        raise ValueError("세트 기본 예상 난이도는 -3.0~3.0이어야 합니다.")
    drafts = build_item_drafts(
        selected,
        metadata_by_source,
        default_target_level,
        default_predicted_difficulty,
    )
    first = selected[0]
    return PublicationSetDraft(
        section=first.section,
        provider_key=first.provider_key,
        model_id=first.model_id,
        backend=first.backend,
        default_target_level=default_target_level,
        default_predicted_difficulty=default_predicted_difficulty,
        items=drafts,
    )


def build_item_drafts(
    candidates: Iterable[PublicationCandidate],
    metadata_by_source: dict[str, PublicationItemMetadata],
    default_target_level: int = 4,
    default_predicted_difficulty: float = 0.0,
) -> tuple[PublicationItemDraft, ...]:
    values = tuple(candidates)
    if not 1 <= default_target_level <= 6:
        raise ValueError("기본 TOPIK 급수는 1~6이어야 합니다.")
    if not -3.0 <= default_predicted_difficulty <= 3.0:
        raise ValueError("기본 예상 난이도는 -3.0~3.0이어야 합니다.")
    drafts: list[PublicationItemDraft] = []
    for candidate in values:
        metadata = metadata_by_source.get(
            candidate.source_key,
            PublicationItemMetadata(
                target_level=default_target_level,
                predicted_difficulty=default_predicted_difficulty,
                primary_skill=default_primary_skill(candidate),
            ),
        )
        metadata.validate()
        stem = display_stem(candidate)
        choices = display_choices(candidate)
        payload = item_payload(candidate, metadata, stem, choices)
        drafts.append(
            PublicationItemDraft(
                candidate=candidate,
                metadata=metadata,
                stem=stem,
                choices=choices,
                stem_length=len(normalize_text(stem)),
                choice_count=len(choices),
                content_hash=_sha256(payload),
            )
        )
    return tuple(drafts)


def reconcile_catalog(
    catalog: CandidateCatalog,
    published_items: Iterable[dict],
    set_source_keys: set[str] | None = None,
) -> list[dict]:
    published = {str(value["source_key"]): value for value in published_items}
    local = {record.candidate.source_key: record for record in catalog.records}
    memberships = set_source_keys or set()
    rows: list[dict] = []
    for source_key, record in local.items():
        candidate = record.candidate
        remote = published.get(source_key)
        if remote is None:
            status = "미발행" if record.eligible else "발행 제외"
        elif not record.eligible:
            status = "발행 후 승인 취소/오류"
        elif _sha256(candidate.question) == _sha256(remote.get("content_json", {})):
            status = "최신"
        else:
            status = "수정 후 미동기"
        rows.append(_reconciliation_row(candidate, record, remote, status, source_key in memberships))
    for source_key, remote in published.items():
        if source_key in local:
            continue
        rows.append(
            {
                "source_key": source_key,
                "section": remote.get("section", ""),
                "provider_key": remote.get("generator_model", ""),
                "model_id": remote.get("generator_version", ""),
                "type_slot": remote.get("type_slot"),
                "item_type": remote.get("item_type", ""),
                "local_id": None,
                "status": "로컬 없음",
                "reason": "로컬 SQLite에서 원본을 찾을 수 없습니다.",
                "item_id": str(remote.get("item_id", "")),
                "item_version": remote.get("item_version"),
                "published_at": remote.get("created_at"),
                "in_set": source_key in memberships,
                "local_question": None,
                "postgres_question": remote.get("content_json", {}),
            }
        )
    rows.sort(key=lambda value: (str(value["section"]), str(value["model_id"]), int(value["type_slot"] or 0), str(value["source_key"])))
    return rows


def display_stem(candidate: PublicationCandidate) -> str:
    question = candidate.question
    if candidate.section == "listening":
        dialogue = "\n".join(
            f"{turn.get('speaker', '').strip()}: {turn.get('text', '').strip()}".strip(": ")
            for turn in question.get("dialogue_turns", [])
            if turn.get("speaker") or turn.get("text")
        )
        return "\n".join(value for value in (dialogue, question.get("question_prompt", "")) if str(value).strip())
    structured = [question.get("auxiliary_text", ""), question.get("passage", ""), question.get("question_prompt", "")]
    if any(str(value).strip() for value in structured):
        return "\n".join(str(value).strip() for value in structured if str(value).strip())
    return str(question.get("stem", "")).strip()


def display_choices(candidate: PublicationCandidate) -> list[str]:
    question = candidate.question
    choices = [str(value) for value in question.get("choices", []) if str(value).strip()]
    if choices:
        return choices
    return [
        str(value.get("description", ""))
        for value in question.get("visual_options", [])
        if str(value.get("description", "")).strip()
    ]


def default_primary_skill(candidate: PublicationCandidate) -> str:
    key = "target_skill" if candidate.section == "listening" else "target_grammar"
    return str(candidate.question.get(key, "")).strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def item_payload(candidate: PublicationCandidate, metadata: PublicationItemMetadata, stem: str, choices: list[str]) -> dict:
    return {
        "section": candidate.section,
        "item_type": candidate.question_type,
        "primary_skill": metadata.primary_skill.strip(),
        "target_level": metadata.target_level,
        "predicted_difficulty": round(float(metadata.predicted_difficulty), 4),
        "generator_provider": candidate.backend,
        "generator_model": candidate.provider_key,
        "generator_version": candidate.model_id,
        "prompt_version": candidate.prompt_version,
        "review_status": "reviewed",
        "stem": stem,
        "choices": choices,
        "correct_answer": candidate.question.get("answer"),
        "explanation": candidate.question.get("explanation", ""),
        "content": candidate.question,
    }


def set_fingerprint(items: Iterable[tuple[uuid.UUID, int]], default_target_level: int, default_predicted_difficulty: float) -> str:
    return _sha256(
        {
            "items": [(str(item_id), version) for item_id, version in items],
            "default_target_level": default_target_level,
            "default_predicted_difficulty": round(float(default_predicted_difficulty), 4),
            "review_status": "reviewed",
        }
    )


def _collect_database(path: Path, root: Path, section: Section, catalog: CandidateCatalog) -> None:
    question_type = path.stem
    profile = QUESTION_TYPE_PROFILES.get(question_type) if section == "reading" else LISTENING_TYPE_PROFILES.get(question_type)
    if profile is None:
        return
    allowed_slots = set(profile.question_numbers)
    try:
        # Publication is a read-only projection over the local authoring stores.
        # URI read-only mode also prevents an accidental journal/schema write.
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT g.id, g.run_id, g.provider, g.model, g.data_json, g.edited_json,
                          g.validation_json, g.created_at, rv.data_json AS review_json,
                          run.prompt_system, run.prompt_user
                   FROM generated_questions g
                   JOIN runs run ON run.id = g.run_id
                   LEFT JOIN reviews rv ON rv.generated_question_id = g.id
                   ORDER BY g.id"""
            ).fetchall()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return
    for row in rows:
        review = _json_object(row["review_json"])
        question = normalize_question_blank_markers(
            _json_object(row["edited_json"] or row["data_json"])
        )
        validation = discard_resolved_validation_issues(
            question, _json_list(row["validation_json"])
        )
        slot = int(question.get("type_slot", 0) or 0)
        reason = ""
        if not review.get("approved"):
            reason = "최종 승인되지 않았습니다."
        elif slot not in allowed_slots:
            reason = f"{question_type} 유형의 담당 번호 {sorted(allowed_slots)}와 문항 번호 {slot}이 일치하지 않습니다."
        elif any(value.get("severity") == "error" for value in validation):
            reason = "자동 검사 오류가 남아 있습니다."
        provider_key = str(row["provider"])
        model_id = str(row["model"])
        candidate = PublicationCandidate(
            section=section,
            question_type=question_type,
            generated_id=int(row["id"]),
            run_id=int(row["run_id"]),
            provider_key=provider_key,
            model_id=model_id,
            backend=provider_backend(provider_key, model_id),
            question=question,
            review=review,
            validation=validation,
            prompt_system=str(row["prompt_system"]),
            prompt_user=str(row["prompt_user"]),
            created_at=str(row["created_at"]),
            source_db=str(path.relative_to(root)).replace("\\", "/"),
        )
        catalog.records.append(LocalPublicationRecord(candidate, not reason, reason))
        if reason:
            catalog.exclusions.append(CandidateExclusion(section, question_type, int(row["id"]), reason))
            continue
        catalog.candidates.append(candidate)


def _reconciliation_row(
    candidate: PublicationCandidate,
    record: LocalPublicationRecord,
    remote: dict | None,
    status: str,
    in_set: bool,
) -> dict:
    return {
        "source_key": candidate.source_key,
        "section": candidate.section,
        "provider_key": candidate.provider_key,
        "model_id": candidate.model_id,
        "type_slot": candidate.slot,
        "item_type": candidate.question_type,
        "local_id": candidate.generated_id,
        "status": status,
        "reason": record.reason,
        "item_id": str(remote.get("item_id", "")) if remote else "",
        "item_version": remote.get("item_version") if remote else None,
        "published_at": remote.get("created_at") if remote else None,
        "in_set": in_set,
        "local_question": candidate.question,
        "postgres_question": remote.get("content_json", {}) if remote else None,
    }


def _json_object(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> list[dict]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
