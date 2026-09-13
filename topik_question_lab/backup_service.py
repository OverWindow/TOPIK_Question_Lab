from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "backup_manifest.json"
ALLOWED_RESTORE_ROOTS = {
    "data",
    "extracted_text",
    "html변환",
    "TOPIK-II-Reading-Test-Paper",
    "TOPIK-II-Listening-Script",
}


@dataclass(frozen=True)
class BackupOptions:
    include_sources: bool = True
    include_rendered_pages: bool = False


@dataclass(frozen=True)
class BackupArtifact:
    filename: str
    data: bytes
    manifest: dict


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _category(relative: Path) -> str:
    value = relative.as_posix()
    if value.startswith("data/listening/types/"):
        return "listening_database"
    if value.startswith("data/types/") or value == "data/topik_lab.db":
        return "reading_database"
    if value == "data/topic_bank.db":
        return "topic_database"
    if value.startswith("data/listening/assets/"):
        return "listening_asset"
    if value.startswith("data/listening/imports/"):
        return "listening_recognition"
    if value.startswith("extracted_text/") or value.startswith("html변환/"):
        return "reading_recognition"
    if value.startswith("TOPIK-II-Listening-Script/"):
        return "listening_source"
    if value.startswith("TOPIK-II-Reading-Test-Paper/"):
        return "reading_source"
    return "other"


def _collect_files(root: Path, options: BackupOptions) -> list[Path]:
    candidates: list[Path] = []
    patterns = [
        "data/types/*.db",
        "data/topik_lab.db",
        "data/topic_bank.db",
        "data/listening/types/*.db",
        "data/listening/assets/**/*",
        "data/listening/imports/*/manifest.json",
        "data/listening/imports/*/questions.json",
        "extracted_text/**/*",
        "html변환/**/*",
    ]
    if options.include_rendered_pages:
        patterns.append("data/listening/imports/*/pages/*.png")
    if options.include_sources:
        patterns.extend(
            [
                "TOPIK-II-Reading-Test-Paper/**/*",
                "TOPIK-II-Listening-Script/**/*",
            ]
        )
    for pattern in patterns:
        candidates.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(candidates), key=lambda value: value.relative_to(root).as_posix())


def _sqlite_snapshot(path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / path.name
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(snapshot)) as destination:
            with destination:
                source.backup(destination)
        return snapshot.read_bytes()


def create_backup(root: Path, options: BackupOptions | None = None) -> BackupArtifact:
    root = root.resolve()
    options = options or BackupOptions()
    entries: list[tuple[str, bytes, str]] = []
    for path in _collect_files(root, options):
        relative = path.relative_to(root)
        data = _sqlite_snapshot(path) if path.suffix.lower() == ".db" else path.read_bytes()
        entries.append((relative.as_posix(), data, _category(relative)))

    created_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "format": "topik-question-lab-backup",
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": created_at,
        "options": {
            "include_sources": options.include_sources,
            "include_rendered_pages": options.include_rendered_pages,
        },
        "files": [
            {"path": name, "size": len(data), "sha256": _sha256(data), "category": category}
            for name, data, category in entries
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, data, _ in entries:
            archive.writestr(name, data)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return BackupArtifact(f"topik_lab_backup_{timestamp}.zip", output.getvalue(), manifest)


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or path.parts[0] not in ALLOWED_RESTORE_ROOTS:
        raise ValueError(f"허용되지 않은 백업 경로입니다: {name}")
    return path


def inspect_backup(data: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if MANIFEST_NAME not in archive.namelist():
                raise ValueError("백업 manifest가 없습니다.")
            manifest = json.loads(archive.read(MANIFEST_NAME))
            if manifest.get("format") != "topik-question-lab-backup":
                raise ValueError("TOPIK Question Lab 백업 파일이 아닙니다.")
            if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
                raise ValueError("지원하지 않는 백업 버전입니다.")
            declared = {item["path"]: item for item in manifest.get("files", [])}
            actual = {name for name in archive.namelist() if name != MANIFEST_NAME and not name.endswith("/")}
            if actual != set(declared):
                raise ValueError("백업 manifest와 ZIP 파일 목록이 일치하지 않습니다.")
            total = 0
            for name, item in declared.items():
                _safe_member(name)
                payload = archive.read(name)
                total += len(payload)
                if total > 2 * 1024 * 1024 * 1024:
                    raise ValueError("백업 압축 해제 크기가 2GB를 초과합니다.")
                if len(payload) != int(item["size"]) or _sha256(payload) != item["sha256"]:
                    raise ValueError(f"백업 파일 무결성 검사에 실패했습니다: {name}")
            return manifest
    except zipfile.BadZipFile as exc:
        raise ValueError("올바른 ZIP 백업 파일이 아닙니다.") from exc


def save_local_backup(root: Path, artifact: BackupArtifact) -> Path:
    target_dir = root.resolve() / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / artifact.filename
    target.write_bytes(artifact.data)
    return target


def restore_backup(root: Path, data: bytes) -> tuple[dict, Path]:
    root = root.resolve()
    manifest = inspect_backup(data)
    safety = create_backup(root, BackupOptions(include_sources=False, include_rendered_pages=False))
    safety_name = safety.filename.replace("topik_lab_backup_", "before_restore_")
    safety = BackupArtifact(safety_name, safety.data, safety.manifest)
    safety_path = save_local_backup(root, safety)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in manifest["files"]:
            member = _safe_member(item["path"])
            target = (root / Path(*member.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"작업 공간 밖으로 복원할 수 없습니다: {member}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = archive.read(item["path"])
            temporary = target.with_name(f".{target.name}.restore_tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, target)
    return manifest, safety_path


def manifest_summary(manifest: dict) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in manifest.get("files", []):
        category = item.get("category", "other")
        summary[category] = summary.get(category, 0) + 1
    return summary
