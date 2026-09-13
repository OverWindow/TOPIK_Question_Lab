from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, Field, field_validator

from .topic_catalog import default_topic_records
from .slot_topic_catalog import default_slot_pool_records


RECENT_TOPIC_LIMIT = 50
TOPIC_STORE_SCHEMA_VERSION = 2
TOPIC_METADATA_FIELDS = ("topic_id", "topic_domain", "topic_title", "topic_angle")
TOPIC_FREE_READING_TYPES = {"grammar_blank", "similar_expression"}
TOPIC_FREE_LISTENING_TYPES = {
    "visual_scene",
    "visual_chart",
    "next_response",
    "followup_action",
    "content_match_once",
    "main_idea_once",
}


def uses_topic_bank(section: str, question_type: str) -> bool:
    if section == "reading":
        return question_type not in TOPIC_FREE_READING_TYPES
    if section == "listening":
        return question_type not in TOPIC_FREE_LISTENING_TYPES
    raise ValueError(f"알 수 없는 영역입니다: {section}")


class TopicDefinition(BaseModel):
    id: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    title: str = Field(min_length=1)
    angles: list[str] = Field(min_length=3, max_length=3)
    reading_types: list[str] = Field(default_factory=list)
    listening_types: list[str] = Field(default_factory=list)
    active: bool = True
    is_default: bool = False

    @field_validator("angles")
    @classmethod
    def clean_angles(cls, values: list[str]) -> list[str]:
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        if len(cleaned) != 3 or len(set(cleaned)) != 3:
            raise ValueError("서로 다른 접근 관점 세 개가 필요합니다.")
        return cleaned


class TopicBrief(BaseModel):
    unit_key: str
    slot_numbers: list[int]
    topic_id: str
    domain: str
    title: str
    angle: str
    source: Literal["global", "slot_pool"] = "global"
    pool_id: str = ""
    prompt_instruction: str = ""

    @property
    def pair(self) -> tuple[str, str]:
        return self.topic_id, self.angle


class TopicUsage(BaseModel):
    topic_id: str
    angle: str
    section: str
    question_type: str
    provider: str
    model: str
    run_id: int
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def parse_pool_items(value: str | list[str]) -> list[str]:
    """Split comma/newline input, preserving commas enclosed by parentheses."""
    if isinstance(value, list):
        value = "\n".join(str(item) for item in value)
    result: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in value.replace("\r\n", "\n").replace("\r", "\n"):
        if char in "([（":
            depth += 1
        elif char in ")]）" and depth:
            depth -= 1
        if (char == "," or char == "\n") and depth == 0:
            item = "".join(buffer).strip()
            if item and item not in result:
                result.append(item)
            buffer = []
        else:
            buffer.append(char)
    item = "".join(buffer).strip()
    if item and item not in result:
        result.append(item)
    return result


def parse_slot_numbers(value: str | list[int]) -> list[int]:
    if isinstance(value, list):
        numbers = [int(number) for number in value]
    else:
        normalized = value.replace("번", "").replace("～", "~")
        normalized = re.sub(r"\s*([~-])\s*", r"\1", normalized)
        numbers = []
        for token in re.split(r"[\s,]+", normalized.strip()):
            if not token:
                continue
            match = re.fullmatch(r"(\d+)\s*[~-]\s*(\d+)", token)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
                if start > end:
                    raise ValueError("문항 번호 범위의 시작은 끝보다 클 수 없습니다.")
                numbers.extend(range(start, end + 1))
            elif token.isdigit():
                numbers.append(int(token))
            else:
                raise ValueError(f"문항 번호 형식을 확인하세요: {token}")
    result = sorted(set(numbers))
    if not result or any(number < 1 or number > 50 for number in result):
        raise ValueError("문항 번호는 1~50 사이에서 하나 이상 지정해야 합니다.")
    return result


def format_slot_numbers(numbers: list[int]) -> str:
    if not numbers:
        return ""
    groups: list[tuple[int, int]] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        groups.append((start, previous))
        start = previous = number
    groups.append((start, previous))
    return ", ".join(str(start) if start == end else f"{start}~{end}" for start, end in groups)


def slot_item_topic_id(pool_id: str, item: str) -> str:
    digest = hashlib.sha256(item.encode("utf-8")).hexdigest()[:12]
    return f"slot_pool:{pool_id}:{digest}"


class SlotTopicPool(BaseModel):
    id: str = Field(min_length=1)
    section: Literal["reading", "listening"]
    slot_numbers: list[int] = Field(min_length=1)
    name: str = Field(min_length=1)
    prompt_instruction: str = Field(min_length=1)
    items: list[str] = Field(min_length=1)
    active: bool = True
    is_default: bool = False
    user_modified: bool = False

    @field_validator("slot_numbers")
    @classmethod
    def clean_slot_numbers(cls, values: list[int]) -> list[int]:
        return parse_slot_numbers(values)

    @field_validator("items", mode="before")
    @classmethod
    def clean_items(cls, value: str | list[str]) -> list[str]:
        return parse_pool_items(value)


class SlotPoolUsage(BaseModel):
    pool_id: str
    item: str
    section: str
    question_type: str
    provider: str
    model: str
    run_id: int
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class InsufficientTopicsError(ValueError):
    pass


class SlotPoolConfigurationError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    title TEXT NOT NULL,
    angles_json TEXT NOT NULL,
    reading_types_json TEXT NOT NULL,
    listening_types_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    user_modified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS topic_usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id TEXT NOT NULL,
    angle TEXT NOT NULL,
    section TEXT NOT NULL,
    question_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES topics(id)
);
CREATE INDEX IF NOT EXISTS idx_topic_usages_recent ON topic_usages(id DESC);
CREATE INDEX IF NOT EXISTS idx_topic_usages_topic ON topic_usages(topic_id);
CREATE TABLE IF NOT EXISTS slot_topic_pools (
    id TEXT PRIMARY KEY,
    section TEXT NOT NULL,
    slot_numbers_json TEXT NOT NULL,
    name TEXT NOT NULL,
    prompt_instruction TEXT NOT NULL,
    items_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    user_modified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS slot_pool_usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id TEXT NOT NULL,
    item TEXT NOT NULL,
    section TEXT NOT NULL,
    question_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(pool_id) REFERENCES slot_topic_pools(id)
);
CREATE INDEX IF NOT EXISTS idx_slot_pool_usages_recent ON slot_pool_usages(id DESC);
CREATE INDEX IF NOT EXISTS idx_slot_pool_usages_pool_item ON slot_pool_usages(pool_id, item);
"""


class TopicStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(topics)").fetchall()}
            if "user_modified" not in columns:
                connection.execute("ALTER TABLE topics ADD COLUMN user_modified INTEGER NOT NULL DEFAULT 0")
        self.seed_defaults()
        self.seed_default_slot_pools()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _row_to_topic(row: sqlite3.Row) -> TopicDefinition:
        return TopicDefinition(
            id=row["id"],
            domain=row["domain"],
            title=row["title"],
            angles=json.loads(row["angles_json"]),
            reading_types=json.loads(row["reading_types_json"]),
            listening_types=json.loads(row["listening_types_json"]),
            active=bool(row["active"]),
            is_default=bool(row["is_default"]),
        )

    @staticmethod
    def _row_to_slot_pool(row: sqlite3.Row) -> SlotTopicPool:
        return SlotTopicPool(
            id=row["id"],
            section=row["section"],
            slot_numbers=json.loads(row["slot_numbers_json"]),
            name=row["name"],
            prompt_instruction=row["prompt_instruction"],
            items=json.loads(row["items_json"]),
            active=bool(row["active"]),
            is_default=bool(row["is_default"]),
            user_modified=bool(row["user_modified"]),
        )

    def seed_defaults(self) -> int:
        inserted = 0
        with self.connect() as connection:
            for raw in default_topic_records():
                topic = TopicDefinition.model_validate(raw)
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO topics(
                           id, domain, title, angles_json, reading_types_json,
                           listening_types_json, active, is_default
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        topic.id,
                        topic.domain,
                        topic.title,
                        json.dumps(topic.angles, ensure_ascii=False),
                        json.dumps(topic.reading_types, ensure_ascii=False),
                        json.dumps(topic.listening_types, ensure_ascii=False),
                        int(topic.active),
                        int(topic.is_default),
                    ),
                )
                inserted += cursor.rowcount
                connection.execute(
                    """UPDATE topics SET
                           reading_types_json = ?, listening_types_json = ?,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND is_default = 1 AND user_modified = 0
                         AND (reading_types_json <> ? OR listening_types_json <> ?)""",
                    (
                        json.dumps(topic.reading_types, ensure_ascii=False),
                        json.dumps(topic.listening_types, ensure_ascii=False),
                        topic.id,
                        json.dumps(topic.reading_types, ensure_ascii=False),
                        json.dumps(topic.listening_types, ensure_ascii=False),
                    ),
                )
        return inserted

    def seed_default_slot_pools(self) -> int:
        """Restore only missing built-in pools; never overwrite an existing row."""
        inserted = 0
        with self.connect() as connection:
            for raw in default_slot_pool_records():
                pool = SlotTopicPool.model_validate({**raw, "is_default": True})
                existing = connection.execute(
                    "SELECT 1 FROM slot_topic_pools WHERE id = ?", (pool.id,)
                ).fetchone()
                if existing:
                    continue
                active_rows = connection.execute(
                    "SELECT slot_numbers_json FROM slot_topic_pools WHERE section = ? AND active = 1",
                    (pool.section,),
                ).fetchall()
                has_overlap = any(
                    set(pool.slot_numbers) & set(json.loads(row["slot_numbers_json"]))
                    for row in active_rows
                )
                cursor = connection.execute(
                    """INSERT INTO slot_topic_pools(
                           id, section, slot_numbers_json, name, prompt_instruction,
                           items_json, active, is_default, user_modified
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pool.id,
                        pool.section,
                        json.dumps(pool.slot_numbers, ensure_ascii=False),
                        pool.name,
                        pool.prompt_instruction,
                        json.dumps(pool.items, ensure_ascii=False),
                        int(pool.active and not has_overlap),
                        1,
                        0,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def list_slot_pools(
        self,
        *,
        section: str = "",
        active_only: bool = False,
    ) -> list[SlotTopicPool]:
        clauses: list[str] = []
        params: list[object] = []
        if section:
            clauses.append("section = ?")
            params.append(section)
        if active_only:
            clauses.append("active = 1")
        sql = "SELECT * FROM slot_topic_pools"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY section, CAST(json_extract(slot_numbers_json, '$[0]') AS INTEGER), name, id"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_slot_pool(row) for row in rows]

    def get_slot_pool(self, pool_id: str) -> SlotTopicPool | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM slot_topic_pools WHERE id = ?", (pool_id,)
            ).fetchone()
        return self._row_to_slot_pool(row) if row else None

    @staticmethod
    def _validate_pool_overlap(
        connection: sqlite3.Connection,
        pool: SlotTopicPool,
    ) -> None:
        if not pool.active:
            return
        rows = connection.execute(
            "SELECT * FROM slot_topic_pools WHERE section = ? AND active = 1 AND id <> ?",
            (pool.section, pool.id),
        ).fetchall()
        requested = set(pool.slot_numbers)
        for row in rows:
            existing = TopicStore._row_to_slot_pool(row)
            overlap = sorted(requested & set(existing.slot_numbers))
            if overlap:
                slots = format_slot_numbers(overlap)
                raise SlotPoolConfigurationError(
                    f"{pool.section} {slots}번은 활성 풀 ‘{existing.name}’에도 포함되어 있습니다. "
                    "기존 풀의 번호를 바꾸거나 비활성화하세요."
                )

    def save_slot_pool(self, pool: SlotTopicPool) -> None:
        with self.connect() as connection:
            self._validate_pool_overlap(connection, pool)
            existing = connection.execute(
                "SELECT is_default FROM slot_topic_pools WHERE id = ?", (pool.id,)
            ).fetchone()
            is_default = bool(existing["is_default"]) if existing else pool.is_default
            connection.execute(
                """INSERT INTO slot_topic_pools(
                       id, section, slot_numbers_json, name, prompt_instruction,
                       items_json, active, is_default, user_modified
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(id) DO UPDATE SET
                       section=excluded.section,
                       slot_numbers_json=excluded.slot_numbers_json,
                       name=excluded.name,
                       prompt_instruction=excluded.prompt_instruction,
                       items_json=excluded.items_json,
                       active=excluded.active,
                       user_modified=1,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    pool.id,
                    pool.section,
                    json.dumps(pool.slot_numbers, ensure_ascii=False),
                    pool.name.strip(),
                    pool.prompt_instruction.strip(),
                    json.dumps(pool.items, ensure_ascii=False),
                    int(pool.active),
                    int(is_default),
                ),
            )

    def set_slot_pool_active(self, pool_id: str, active: bool) -> None:
        pool = self.get_slot_pool(pool_id)
        if pool is None:
            raise ValueError(f"소재 풀을 찾을 수 없습니다: {pool_id}")
        self.save_slot_pool(pool.model_copy(update={"active": active}))

    def has_active_slot_pool(self, section: str, slot_numbers: tuple[int, ...] | list[int]) -> bool:
        requested = set(slot_numbers)
        return any(
            requested & set(pool.slot_numbers)
            for pool in self.list_slot_pools(section=section, active_only=True)
        )

    def _pool_for_unit(self, section: str, slot_numbers: list[int]) -> SlotTopicPool | None:
        pools = self.list_slot_pools(section=section, active_only=True)
        by_slot: list[SlotTopicPool | None] = []
        for slot in slot_numbers:
            matches = [pool for pool in pools if slot in pool.slot_numbers]
            if len(matches) > 1:
                raise SlotPoolConfigurationError(
                    f"{section} {slot}번에 활성 사용자 지정 풀이 둘 이상 겹칩니다."
                )
            by_slot.append(matches[0] if matches else None)
        selected = {pool.id for pool in by_slot if pool is not None}
        if not selected:
            return None
        if len(selected) != 1 or any(pool is None for pool in by_slot):
            slots = format_slot_numbers(slot_numbers)
            raise SlotPoolConfigurationError(
                f"공통 세트 {section} {slots}번의 모든 문항에는 동일한 사용자 지정 풀이 필요합니다. "
                "적용 번호를 세트 전체로 맞추거나 풀을 비활성화하세요."
            )
        return next(pool for pool in by_slot if pool is not None)

    def list_topics(
        self,
        *,
        active_only: bool = False,
        query: str = "",
        domain: str = "",
    ) -> list[TopicDefinition]:
        clauses: list[str] = []
        params: list[object] = []
        if active_only:
            clauses.append("active = 1")
        if query.strip():
            clauses.append("(title LIKE ? OR domain LIKE ? OR angles_json LIKE ?)")
            pattern = f"%{query.strip()}%"
            params.extend([pattern, pattern, pattern])
        if domain.strip():
            clauses.append("domain = ?")
            params.append(domain.strip())
        sql = "SELECT * FROM topics"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY domain, title, id"
        with self.connect() as connection:
            return [self._row_to_topic(row) for row in connection.execute(sql, params).fetchall()]

    def get_topic(self, topic_id: str) -> TopicDefinition | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        return self._row_to_topic(row) if row else None

    def save_topic(self, topic: TopicDefinition) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO topics(
                       id, domain, title, angles_json, reading_types_json,
                       listening_types_json, active, is_default
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       domain=excluded.domain,
                       title=excluded.title,
                       angles_json=excluded.angles_json,
                       reading_types_json=excluded.reading_types_json,
                       listening_types_json=excluded.listening_types_json,
                       active=excluded.active,
                       user_modified=1,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    topic.id,
                    topic.domain,
                    topic.title,
                    json.dumps(topic.angles, ensure_ascii=False),
                    json.dumps(topic.reading_types, ensure_ascii=False),
                    json.dumps(topic.listening_types, ensure_ascii=False),
                    int(topic.active),
                    int(topic.is_default),
                ),
            )

    def set_active(self, topic_id: str, active: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE topics SET active = ?, updated_at=CURRENT_TIMESTAMP WHERE id = ?",
                (int(active), topic_id),
            )

    def recent_pairs(self, limit: int = RECENT_TOPIC_LIMIT) -> set[tuple[str, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT topic_id, angle FROM topic_usages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {(str(row["topic_id"]), str(row["angle"])) for row in rows}

    def usage_counts(self) -> tuple[Counter[str], Counter[str]]:
        topic_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.topic_id, t.domain, COUNT(*) AS usage_count
                   FROM topic_usages u JOIN topics t ON t.id = u.topic_id
                   GROUP BY u.topic_id, t.domain"""
            ).fetchall()
        for row in rows:
            count = int(row["usage_count"])
            topic_counts[str(row["topic_id"])] = count
            domain_counts[str(row["domain"])] += count
        return topic_counts, domain_counts

    def slot_pool_usage_counts(self, pool_id: str) -> Counter[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT item, COUNT(*) AS usage_count
                   FROM slot_pool_usages WHERE pool_id = ? GROUP BY item""",
                (pool_id,),
            ).fetchall()
        return Counter({str(row["item"]): int(row["usage_count"]) for row in rows})

    def record_usages(
        self,
        briefs: list[TopicBrief],
        *,
        section: str,
        question_type: str,
        provider: str,
        model: str,
        run_id: int,
    ) -> None:
        global_briefs = {
            (brief.topic_id, brief.angle): brief
            for brief in briefs
            if brief.source == "global"
        }
        pool_briefs = {
            (brief.pool_id, brief.title): brief
            for brief in briefs
            if brief.source == "slot_pool" and brief.pool_id
        }
        with self.connect() as connection:
            for brief in global_briefs.values():
                connection.execute(
                    """INSERT INTO topic_usages(
                           topic_id, angle, section, question_type, provider, model, run_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        brief.topic_id,
                        brief.angle,
                        section,
                        question_type,
                        provider,
                        model,
                        run_id,
                    ),
                )
            for brief in pool_briefs.values():
                connection.execute(
                    """INSERT INTO slot_pool_usages(
                           pool_id, item, section, question_type, provider, model, run_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        brief.pool_id,
                        brief.title,
                        section,
                        question_type,
                        provider,
                        model,
                        run_id,
                    ),
                )

    def list_usages(self, limit: int = 100) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.*, t.domain, t.title
                   FROM topic_usages u JOIN topics t ON t.id = u.topic_id
                   ORDER BY u.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_slot_pool_usages(self, limit: int = 100) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.*, p.name AS pool_name
                   FROM slot_pool_usages u
                   JOIN slot_topic_pools p ON p.id = u.pool_id
                   ORDER BY u.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def selection_revision(self) -> str:
        with self.connect() as connection:
            topic_rows = connection.execute(
                """SELECT id, domain, title, angles_json, reading_types_json,
                          listening_types_json, active, updated_at
                   FROM topics ORDER BY id"""
            ).fetchall()
            usage_row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest_id FROM topic_usages"
            ).fetchone()
            pool_rows = connection.execute(
                """SELECT id, section, slot_numbers_json, name, prompt_instruction,
                          items_json, active, updated_at
                   FROM slot_topic_pools ORDER BY id"""
            ).fetchall()
            pool_usage_row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest_id FROM slot_pool_usages"
            ).fetchone()
        payload = [tuple(row) for row in topic_rows]
        payload.append(("usage", int(usage_row["latest_id"])))
        payload.extend(("pool", *tuple(row)) for row in pool_rows)
        payload.append(("pool_usage", int(pool_usage_row["latest_id"])))
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()

    def select_plan(
        self,
        *,
        section: str,
        question_type: str,
        units: list[tuple[str, list[int]]],
        forbidden_pairs: set[tuple[str, str]] | None = None,
        initial_pair_counts: Counter[tuple[str, str]] | None = None,
        forbidden_topic_ids: set[str] | None = None,
        initial_domain_counts: Counter[str] | None = None,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> list[TopicBrief]:
        if section not in {"reading", "listening"}:
            raise ValueError(f"알 수 없는 영역입니다: {section}")

        chooser = rng or random.SystemRandom()
        externally_forbidden_pairs = set(forbidden_pairs or set())
        external_pair_counts = Counter(initial_pair_counts or {})
        for pair in externally_forbidden_pairs:
            external_pair_counts[pair] = max(1, external_pair_counts[pair])
        unavailable_topic_ids = set(forbidden_topic_ids or set())
        unit_pools = [
            (unit_key, slot_numbers, self._pool_for_unit(section, slot_numbers))
            for unit_key, slot_numbers in units
        ]
        global_units = [
            (unit_key, slot_numbers)
            for unit_key, slot_numbers, pool in unit_pools
            if pool is None and uses_topic_bank(section, question_type)
        ]

        global_by_key: dict[str, TopicBrief] = {}
        if global_units:
            topics = [
                topic
                for topic in self.list_topics(active_only=True)
                if question_type in (
                    topic.reading_types if section == "reading" else topic.listening_types
                )
            ]
            unavailable_pairs = self.recent_pairs() | externally_forbidden_pairs
            available_topics = [
                topic
                for topic in topics
                if topic.id not in unavailable_topic_ids
                and any((topic.id, angle) not in unavailable_pairs for angle in topic.angles)
            ]
            if len(available_topics) < len(global_units):
                raise InsufficientTopicsError(
                    f"{section}/{question_type}에 사용할 수 있는 서로 다른 공용 소재가 "
                    f"{len(available_topics)}개뿐입니다. {len(global_units)}개가 필요합니다. "
                    "소재를 추가·활성화하거나 최근 사용 이력이 줄어든 뒤 다시 시도하세요."
                )

            topic_usage, domain_usage = self.usage_counts()
            current_domains = Counter(initial_domain_counts or {})
            selected_topic_ids: set[str] = set()
            for unit_key, slot_numbers in global_units:
                candidates = [
                    topic for topic in available_topics if topic.id not in selected_topic_ids
                ]
                minimum_domain_count = min(current_domains[topic.domain] for topic in candidates)
                balanced = [
                    topic
                    for topic in candidates
                    if current_domains[topic.domain] == minimum_domain_count
                ]
                weights = [
                    1.0 / ((1 + topic_usage[topic.id]) * (1 + domain_usage[topic.domain]))
                    for topic in balanced
                ]
                topic = chooser.choices(balanced, weights=weights, k=1)[0]
                angles = [
                    angle
                    for angle in topic.angles
                    if (topic.id, angle) not in unavailable_pairs
                ]
                angle = chooser.choice(angles)
                global_by_key[unit_key] = TopicBrief(
                    unit_key=unit_key,
                    slot_numbers=slot_numbers,
                    topic_id=topic.id,
                    domain=topic.domain,
                    title=topic.title,
                    angle=angle,
                )
                selected_topic_ids.add(topic.id)
                current_domains[topic.domain] += 1
                unavailable_pairs.add((topic.id, angle))

        pool_usage = {
            pool.id: self.slot_pool_usage_counts(pool.id)
            for _, _, pool in unit_pools
            if pool is not None
        }
        selected_pool_counts: Counter[tuple[str, str]] = Counter()
        reserved_counts_by_id: Counter[str] = Counter()
        for (topic_id, _), count in external_pair_counts.items():
            reserved_counts_by_id[topic_id] += count
        reserved_ids = set(reserved_counts_by_id) | unavailable_topic_ids
        result: list[TopicBrief] = []
        for unit_key, slot_numbers, pool in unit_pools:
            if pool is None:
                brief = global_by_key.get(unit_key)
                if brief is not None:
                    result.append(brief)
                continue

            item_ids = {item: slot_item_topic_id(pool.id, item) for item in pool.items}
            fresh = [item for item in pool.items if item_ids[item] not in reserved_ids]
            candidates = fresh or list(pool.items)
            effective_counts = {
                item: pool_usage[pool.id][item]
                + selected_pool_counts[(pool.id, item)]
                + reserved_counts_by_id[item_ids[item]]
                for item in candidates
            }
            minimum = min(effective_counts.values())
            balanced = [item for item in candidates if effective_counts[item] == minimum]
            item = chooser.choice(balanced)
            topic_id = item_ids[item]
            brief = TopicBrief(
                unit_key=unit_key,
                slot_numbers=slot_numbers,
                topic_id=topic_id,
                domain=pool.name,
                title=item,
                angle=pool.prompt_instruction,
                source="slot_pool",
                pool_id=pool.id,
                prompt_instruction=pool.prompt_instruction,
            )
            result.append(brief)
            selected_pool_counts[(pool.id, item)] += 1
            reserved_ids.add(topic_id)
        return result


def generation_units(question_numbers: tuple[int, ...], count: int, shared: bool) -> list[tuple[str, list[int]]]:
    if shared:
        set_count = max(1, count // len(question_numbers))
        return [(f"set-{index}", list(question_numbers)) for index in range(1, set_count + 1)]
    counts = {slot: count // len(question_numbers) for slot in question_numbers}
    for slot in question_numbers[: count % len(question_numbers)]:
        counts[slot] += 1
    units: list[tuple[str, list[int]]] = []
    index = 1
    for slot in question_numbers:
        for _ in range(counts[slot]):
            units.append((f"item-{index}", [slot]))
            index += 1
    return units


def topic_plan_prompt(briefs: list[TopicBrief], *, shared: bool) -> str:
    if not briefs:
        return ""
    rows = [
        {
            "unit_key": brief.unit_key,
            "type_slots": brief.slot_numbers,
            "topic_id": brief.topic_id,
            "source": "문항별 지정 풀" if brief.source == "slot_pool" else "공용 소재 은행",
            "domain": brief.domain,
            "topic": brief.title,
            "angle": brief.angle,
            **(
                {"pool_instruction": brief.prompt_instruction}
                if brief.source == "slot_pool" and brief.prompt_instruction
                else {}
            ),
        }
        for brief in briefs
    ]
    set_instruction = (
        "공통 지문·대본 세트의 set_id는 unit_key를 정확히 사용하고 같은 세트의 모든 문항에 같은 소재를 적용합니다."
        if shared
        else "표의 순서와 type_slots 배정에 맞춰 각 문항에 하나의 소재를 적용합니다."
    )
    return f"""

이번 생성의 소재 배정표:
{json.dumps(rows, ensure_ascii=False, indent=2)}

소재 적용 규칙:
- {set_instruction}
- 배정된 소재와 접근 관점을 중심 내용으로 사용하고 다른 행의 소재를 섞지 않습니다.
- 문항별 지정 풀의 pool_instruction이 있으면 해당 문항의 형식 지침으로 반드시 따릅니다.
- 소재명을 제목처럼 그대로 반복하지 말고 자연스러운 상황·설명·서사로 구체화합니다.
- 각 문항에 topic_id, topic_domain, topic_title, topic_angle을 배정표와 정확히 같게 출력합니다.
""".rstrip()


@dataclass
class TopicPlanApplication:
    payload: dict
    applied_briefs: list[TopicBrief]
    issue_messages: list[list[str]]


def clear_topic_metadata(question: dict) -> None:
    for field in TOPIC_METADATA_FIELDS:
        question[field] = ""


def apply_topic_plan(payload: dict, briefs: list[TopicBrief], *, shared: bool) -> TopicPlanApplication:
    if not isinstance(payload, dict):
        raise ValueError("생성 응답은 JSON 객체여야 합니다.")
    updated = copy.deepcopy(payload)
    raw_questions = updated.get("questions")
    if not isinstance(raw_questions, list):
        return TopicPlanApplication(updated, [], [])
    messages: list[list[str]] = [[] for _ in raw_questions]
    applied: list[TopicBrief] = []
    if not briefs:
        for question in raw_questions:
            if isinstance(question, dict):
                clear_topic_metadata(question)
        return TopicPlanApplication(updated, applied, messages)

    assignments: dict[int, TopicBrief] = {}
    if shared:
        group_indexes: dict[str, list[int]] = {}
        for index, question in enumerate(raw_questions):
            set_id = str(question.get("set_id", ""))
            group_indexes.setdefault(set_id or f"__missing_{index}", []).append(index)
        unused = list(briefs)
        for set_id, indexes in group_indexes.items():
            matching = next((brief for brief in unused if brief.unit_key == set_id), None)
            brief = matching or (unused[0] if unused else None)
            if brief is None:
                messages[indexes[0]].append("생성된 세트에 대응하는 소재 배정이 없습니다.")
                for index in indexes:
                    clear_topic_metadata(raw_questions[index])
                continue
            if matching is None:
                messages[indexes[0]].append(
                    f"세트 ID '{set_id}'가 소재 배정 키와 달라 순서대로 {brief.unit_key} 소재를 연결했습니다."
                )
            unused.remove(brief)
            for index in indexes:
                assignments[index] = brief
            applied.append(brief)
        if unused and raw_questions:
            messages[0].append(f"소재가 배정된 세트 {len(unused)}개가 생성되지 않았습니다.")
    else:
        unused = list(briefs)
        for index, question in enumerate(raw_questions):
            supplied_topic_id = str(question.get("topic_id", ""))
            matching = next(
                (
                    brief
                    for brief in unused
                    if supplied_topic_id and brief.topic_id == supplied_topic_id
                ),
                None,
            )
            if matching is None:
                slot = int(question.get("type_slot", 0) or 0)
                matching = next((brief for brief in unused if slot in brief.slot_numbers), None)
            slot = int(question.get("type_slot", 0) or 0)
            brief = matching or (unused[0] if unused and slot <= 0 else None)
            if brief is None:
                clear_topic_metadata(question)
                continue
            unused.remove(brief)
            assignments[index] = brief
            applied.append(brief)
        if unused and raw_questions:
            messages[0].append(f"배정된 소재 {len(unused)}개에 해당하는 문항이 생성되지 않았습니다.")

    for index, brief in assignments.items():
        question = raw_questions[index]
        slot = int(question.get("type_slot", 0) or 0)
        if slot not in brief.slot_numbers:
            messages[index].append(
                f"문항 슬롯 {slot}번이 소재 배정 슬롯 {brief.slot_numbers}과 일치하지 않습니다."
            )
        supplied_topic_id = str(question.get("topic_id", ""))
        if supplied_topic_id and supplied_topic_id != brief.topic_id:
            messages[index].append(
                f"모델이 반환한 소재 ID({supplied_topic_id})가 배정값({brief.topic_id})과 달라 배정값으로 교정했습니다."
            )
        question.update(
            {
                "topic_id": brief.topic_id,
                "topic_domain": brief.domain,
                "topic_title": brief.title,
                "topic_angle": brief.angle,
            }
        )
    return TopicPlanApplication(updated, applied, messages)
