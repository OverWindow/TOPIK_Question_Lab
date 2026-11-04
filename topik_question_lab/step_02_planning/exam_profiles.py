"""Exam layouts; IBT rules are loaded from the reviewed source mapping."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path


MAPPING_PATH = Path(__file__).resolve().parents[2] / "IBT/type_mapping.json"


def exam_for_type(type_id: str) -> str:
    return "ibt" if type_id.startswith("ibt_") else "pbt"


def question_count(exam_format: str = "pbt") -> int:
    if exam_format not in {"pbt", "ibt"}:
        raise ValueError("시험 형식은 PBT 또는 IBT여야 합니다.")
    return 30 if exam_format == "ibt" else 50


@dataclass(frozen=True)
class IBTProfile:
    type_id: str
    section: str
    base_type_id: str
    label: str
    question_numbers: tuple[int, ...]
    roles: tuple[str, ...]
    generation_rules: tuple[str, ...]
    analysis_focus: str
    content_mode: str
    shared_passage: bool
    shared_script: bool
    highlight_numbers: tuple[int, ...]
    visual_kind: str
    material_kind: str
    topic_slots: tuple[int, ...]
    mapping_hash: str
    exam_format: str = "ibt"
    implemented: bool = True
    skip_undisclosed: bool = True

    @property
    def number_range(self):
        numbers = self.question_numbers
        return str(numbers[0]) if len(numbers) == 1 else f"{numbers[0]}~{numbers[-1]}"

    @property
    def repeat_count(self):
        return 2 if self.shared_script and len(self.question_numbers) == 2 else 1


def ibt_profiles(section: str, path: Path = MAPPING_PATH) -> dict[str, IBTProfile]:
    from .prompt_profiles import QUESTION_TYPE_PROFILES
    from .listening_profiles import LISTENING_TYPE_PROFILES
    if section not in {"reading", "listening"}:
        raise ValueError("읽기 또는 듣기를 선택하세요.")
    raw = path.read_bytes()
    mapping = json.loads(raw)
    if mapping.get("schema_version") != 1 or mapping.get("exam_format") != "ibt":
        raise ValueError("지원하지 않는 IBT 매핑 형식입니다.")
    bases = QUESTION_TYPE_PROFILES if section == "reading" else LISTENING_TYPE_PROFILES
    result = {}
    questions = [q for q in mapping["questions"] if q["section"] == section]
    if sorted(q["ibt_slot"] for q in questions) != list(range(1, 31)):
        raise ValueError("IBT 매핑에 1~30번이 각각 한 번 있어야 합니다.")
    for entry in mapping["types"]:
        if entry["section"] != section:
            continue
        base = bases[entry["base_pbt_type_id"]]
        slots = tuple(entry["ibt_slots"])
        members = sorted((q for q in questions if q["mapping_type_id"] == entry["mapping_type_id"]), key=lambda q: q["ibt_slot"])
        if [q["ibt_slot"] for q in members] != list(slots) or len(entry["roles_in_slot_order"]) != len(slots):
            raise ValueError("IBT 유형별 번호·역할이 일치하지 않습니다.")
        shared = entry["shared_stimulus"]
        if shared and len(slots) != 2:
            raise ValueError("IBT 공통 지문·대본은 두 문항이어야 합니다.")
        for q in members:
            expected = [n for n in slots if n != q["ibt_slot"]] if shared else []
            if q["shared_with_slots"] != expected:
                raise ValueError("IBT 공유 그룹이 유형 구성과 다릅니다.")
        type_id = "ibt_" + entry["mapping_type_id"].split(".", 1)[1]
        visual = entry["visual_kind"]
        # Topic pools belong to PBT semantic roles, not the new IBT numbers.
        refs = entry["pbt_role_slots"]
        topic_slots = tuple(refs[min(i, len(refs) - 1)] for i in range(len(slots)))
        if section == "reading" and entry["mapping_type_id"] == "reading.short_content_match":
            topic_slots = (11, 12)
        result[type_id] = IBTProfile(
            type_id, section, base.type_id, entry["label"], slots,
            tuple(entry["roles_in_slot_order"]),
            tuple(entry["generation_rules"] + entry["differences_from_pbt"]),
            entry["mapping_basis"], getattr(base, "content_mode", "dialogue"),
            shared and section == "reading", shared and section == "listening",
            tuple(q["ibt_slot"] for q in members if q["highlight_required"]),
            {"choice_scene": "scene", "choice_chart": "chart"}.get(visual, "none"),
            visual if visual.startswith("material_") else "none", topic_slots,
            hashlib.sha256(raw).hexdigest(),
        )
    if sorted(n for p in result.values() for n in p.question_numbers) != list(range(1, 31)):
        raise ValueError("IBT 유형 구성이 1~30번을 정확히 포함하지 않습니다.")
    return result


def profiles_for_exam(section: str, exam_format: str = "pbt"):
    question_count(exam_format)
    if exam_format == "ibt":
        return ibt_profiles(section)
    from .prompt_profiles import QUESTION_TYPE_PROFILES
    from .listening_profiles import LISTENING_TYPE_PROFILES
    if section == "reading":
        return {k: v for k, v in QUESTION_TYPE_PROFILES.items() if v.implemented}
    if section == "listening":
        return LISTENING_TYPE_PROFILES
    raise ValueError("읽기 또는 듣기를 선택하세요.")


def profile_for(section: str, type_id: str, snapshot: dict | None = None):
    if snapshot:
        value = dict(snapshot)
        for key in ("question_numbers", "roles", "generation_rules", "highlight_numbers", "topic_slots"):
            value[key] = tuple(value[key])
        profile = IBTProfile(**value)
        if profile.type_id != type_id or profile.section != section:
            raise ValueError("저장된 시험 구성과 문항 유형이 다릅니다.")
        return profile
    return profiles_for_exam(section, exam_for_type(type_id))[type_id]


def profile_snapshot(profile) -> dict | None:
    return asdict(profile) if isinstance(profile, IBTProfile) else None


def requires_material(section, slot, question):
    if section != "reading":
        return False
    if question.get("exam_format", "pbt") == "pbt":
        return slot == 10
    profile = profile_for(section, question["question_type"], question.get("generation_profile"))
    return profile.material_kind != "none"
