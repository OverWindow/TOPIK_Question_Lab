"""Read-only checks of complete local sets and their media files."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from topik_question_lab.step_06_publication.postgres_storage import _psycopg, _dict_rows

MEDIA_TABLES = ("tts_audio_assets", "item_audio_bindings", "item_visual_assets", "question_set_item_audio_bindings")
ASSET_TABLES = ("tts_audio_assets", "item_visual_assets")


def object_path(root: Path, bucket: str, path: str) -> Path:
    parts = [bucket, *path.split("/")]
    if any(not p or p in {".", ".."} or any(c in p for c in "\\:\0/") for p in parts):
        raise ValueError("미디어 저장 경로가 올바르지 않습니다.")
    resolved_root = root.resolve()
    target = resolved_root.joinpath(*parts).resolve()
    if not target.is_relative_to(resolved_root):
        raise ValueError("미디어 경로가 로컬 저장소 밖을 참조합니다.")
    return target


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class MediaManifest:
    files: tuple[dict, ...]

    @property
    def byte_size(self):
        return sum(row["byte_size"] for row in self.files)


class LocalMediaValidator:
    def __init__(self, source_root: Path):
        self.source_root = Path(source_root)

    def prepare(self, rows) -> MediaManifest:
        """Validate every selected set, including required assets and local file bytes."""
        items = rows["items"]
        audio = {str(r["audio_asset_id"]): r for r in rows["tts_audio_assets"] if r["deleted_at"] is None}
        set_audio = {(str(r["set_id"]), r["position"]): r for r in rows["question_set_item_audio_bindings"]}
        item_audio = {(str(r["item_id"]), r["content_hash"]): r for r in rows["item_audio_bindings"]}
        visuals = {(str(r["item_id"]), r["content_hash"], r["visual_role"], r["option_number"]): r for r in rows["item_visual_assets"]}
        for set_row in rows["question_sets"]:
            members = [i for i in items if i["set_id"] == set_row["set_id"]]
            if len(members) != 50 or {i["position"] for i in members} != set(range(1, 51)):
                raise ValueError("완성 세트 검증에는 중복 없는 1~50번 문항이 필요합니다.")
            if set_row["review_status"] == "retired":
                raise ValueError("퇴역 처리된 세트는 완성 검증을 통과할 수 없습니다.")
            for item in members:
                content = item["content_json"]
                options = content.get("visual_options") or []
                if item["review_status"] == "retired" or item["correct_answer"] not in (1, 2, 3, 4) or (len(item["choices"]) != 4 and len(options) != 4):
                    raise ValueError("유효한 보기·정답과 검수된 문항이 필요합니다.")
                if item["section"] == "listening":
                    binding = set_audio.get((str(item["set_id"]), item["position"])) or item_audio.get((str(item["item_id"]), item["content_hash"]))
                    asset = audio.get(str(binding["audio_asset_id"])) if binding else None
                    if not asset or asset["narration_version"] != "exam_track_v4":
                        raise ValueError(f"듣기 {item['position']}번의 현재 시험 음성이 준비되지 않았습니다.")
                expected = [("choice", n) for n in range(1, len(options) + 1)]
                if item["section"] == "reading" and item["position"] == 10:
                    expected.append(("material", 1))
                if any((str(item["item_id"]), item["content_hash"], role, number) not in visuals for role, number in expected):
                    raise ValueError(f"{item['position']}번의 필수 이미지가 준비되지 않았습니다.")
        files = {}
        for table in ASSET_TABLES:
            for row in rows[table]:
                self._validate_storage(row)
                self._read(row)
                key = row["storage_bucket"], row["storage_path"]
                if key in files and files[key]["file_sha256"] != row["file_sha256"]:
                    raise ValueError("같은 미디어 경로에 서로 다른 파일 해시가 연결되어 있습니다.")
                files[key] = row
        return MediaManifest(tuple(files[key] for key in sorted(files)))

    def _validate_storage(self, row):
        if row["storage_backend"] != "local" or not row["file_sha256"]:
            raise ValueError("기존 원격 미디어의 로컬 파일·해시가 확보되지 않았습니다. 원격 파일은 변경하지 않았습니다.")

    def _read(self, row):
        path = object_path(self.source_root, row["storage_bucket"], row["storage_path"])
        if not path.is_file():
            raise ValueError("검증할 로컬 미디어 파일이 없습니다.")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError("로컬 미디어 파일을 읽지 못했습니다.") from exc
        if len(data) != row["byte_size"] or file_digest(data) != row["file_sha256"]:
            raise ValueError("로컬 미디어 파일의 크기·해시가 DB와 다릅니다. 다시 검수하세요.")
        return data


def preview_local_media(database_url, set_ids, root):
    return preview_media(database_url, set_ids, LocalMediaValidator(Path(root)))


def preview_media(database_url, set_ids, validator):
    selected = list(dict.fromkeys(str(uuid.UUID(str(value))) for value in set_ids))
    if not selected:
        raise ValueError("검증할 세트를 하나 이상 선택하세요.")
    psycopg, _ = _psycopg()
    try:
        with psycopg.connect(database_url, connect_timeout=8, prepare_threshold=None,
                             options="-c default_transaction_read_only=on -c statement_timeout=30000") as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

            def query(statement, params):
                cursor = c.execute(statement, params)
                return _dict_rows(cursor, cursor.fetchall())

            rows = {
                "question_sets": query("SELECT * FROM topik_bank.question_sets WHERE set_id=ANY(%s::uuid[])", (selected,)),
                "items": query("SELECT * FROM topik_bank.items WHERE set_id=ANY(%s::uuid[])", (selected,)),
                "question_set_item_audio_bindings": query(
                    "SELECT * FROM topik_bank.question_set_item_audio_bindings WHERE is_current AND set_id=ANY(%s::uuid[])", (selected,)),
            }
            if len(rows["question_sets"]) != len(selected):
                raise ValueError("로컬에 없는 세트가 선택되었습니다.")
            for table in ("item_audio_bindings", "item_visual_assets"):
                rows[table] = query(
                    f"""SELECT a.* FROM topik_bank.{table} a JOIN topik_bank.items i
                        ON i.item_id=a.item_id AND i.content_hash=a.content_hash
                        WHERE a.is_current AND i.set_id=ANY(%s::uuid[])""", (selected,))
            asset_ids = list({str(row["audio_asset_id"]) for table in
                             ("item_audio_bindings", "question_set_item_audio_bindings") for row in rows[table]})
            rows["tts_audio_assets"] = query(
                "SELECT * FROM topik_bank.tts_audio_assets WHERE audio_asset_id=ANY(%s::uuid[]) AND deleted_at IS NULL", (asset_ids,))
    except psycopg.Error as exc:
        raise ValueError("로컬 DB 연결과 미디어 스키마 준비 상태를 확인하세요.") from exc
    manifest = validator.prepare(rows)
    signature = [(row["storage_bucket"], row["storage_path"], row["file_sha256"]) for row in manifest.files]
    digest = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"sets": len(selected), "items": len(rows["items"]), "files": len(manifest.files),
            "byte_size": manifest.byte_size, "manifest_hash": digest}
