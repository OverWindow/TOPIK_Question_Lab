"""Checkpointed media creation, verified uploads, and atomic bank registration."""
from __future__ import annotations

from collections import defaultdict
import io
import json
import math
import os
from pathlib import Path
import uuid

from PIL import Image

from topik_question_lab.common.listening_models import ChartSpec
from topik_question_lab.common.listening_visuals import render_chart
from topik_question_lab.step_03_generation.audio_composer import COMPOSER_VERSION, compose, narration_script, pcm, validate_mp3
from topik_question_lab.step_03_generation.media_provider import GoogleMediaClient, MediaSettings, RequestRunner, Stopped
from topik_question_lab.step_06_publication.direct_publication import _hash, _json, package_draft
from topik_question_lab.step_06_publication.media_cloud import S3MediaStore, cloudfront_url, destination_key, digest_file
from topik_question_lab.step_06_publication.media_registration import MediaRegistration
from topik_question_lab.step_06_publication.publication import PUBLICATION_NAMESPACE


def chart_spec(value, source=""):
    spec = ChartSpec.model_validate(value)
    if not spec.labels or len(spec.labels) != len(spec.values) or not all(math.isfinite(v) and v >= 0 for v in spec.values):
        raise ValueError("도표의 항목·수치가 없거나 올바르지 않습니다.")
    if source:
        if not spec.title or spec.title not in source or any(label not in source for label in spec.labels):
            raise ValueError("읽기 도표의 제목·항목이 지문과 일치하지 않습니다.")
        import re
        numbers = {float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", source)}
        if any(v not in numbers for v in spec.values) or (spec.unit and spec.unit not in source):
            raise ValueError("읽기 도표의 수치·단위가 지문과 일치하지 않습니다.")
        for label, value in zip(spec.labels, spec.values):
            number = re.escape(f"{value:g}") + (r"(?:\.0+)?" if value == int(value) else "")
            if not re.search(re.escape(label) + r"\s*[:：(\[]?\s*" + number + r"\s*" + re.escape(spec.unit), source):
                raise ValueError("읽기 도표의 항목별 수치가 지문과 일치하지 않습니다. '항목: 수치단위'로 작성하세요.")
    return spec.model_dump()


def media_plan(package, settings):
    draft = package_draft(package)
    groups = defaultdict(list)
    tasks = []
    for item in draft.items:
        candidate, question = item.candidate, item.candidate.question
        if candidate.section == "listening":
            key = candidate.slot if candidate.slot <= 20 else 21 + (candidate.slot - 21) // 2 * 2
            groups[key].append(candidate)
        options = question.get("visual_options") or []
        for option in options:
            number = option["number"]
            if number not in (1, 2, 3, 4):
                raise ValueError("이미지 선택지 번호가 올바르지 않습니다.")
            spec = chart_spec(option.get("chart_spec")) if question.get("visual_kind") == "chart" else None
            prompt = (option.get("image_prompt") or option.get("description") or "").strip()
            if not spec and not prompt:
                raise ValueError("필수 이미지 설명이 없습니다.")
            tasks.append({"kind": "image", "item_id": str(candidate.item_id), "content_hash": item.content_hash,
                          "visual_role": "choice", "option_number": number, "prompt": prompt, "chart_spec": spec})
        if options and {o["number"] for o in options} != {1, 2, 3, 4}:
            raise ValueError("선택지 이미지 네 장이 모두 필요합니다.")
        if candidate.section == "reading" and candidate.slot == 10:
            material = question.get("visual_material") or {}
            source = question.get("passage") or question.get("stem", "")
            spec = chart_spec(material.get("chart_spec"), source)
            tasks.append({"kind": "image", "item_id": str(candidate.item_id), "content_hash": item.content_hash,
                          "visual_role": "material", "option_number": 1, "chart_spec": spec, "prompt": ""})
    for items in groups.values():
        script = narration_script(items)
        tasks.append({"kind": "audio", "script": script, "positions": script["positions"]})
    return tasks


def preflight(root, settings, section, target_url):
    settings.validate(section)
    registration = MediaRegistration(root, target_url)
    registration.preflight()
    client = GoogleMediaClient(settings)
    try:
        client.authenticate()
        store = S3MediaStore(settings.bucket, settings.base_url, region=settings.region)
        try:
            store.s3.head_bucket(Bucket=settings.bucket)
        except Exception:
            raise ValueError("S3 인증·버킷 접근 권한을 확인하세요.") from None
        finally:
            store.close()
    finally:
        client.close()
    return {"settings": settings.snapshot(), "target": registration.target}


class MediaPipeline:
    def __init__(self, root, store, batch_id, config, *, stop=None, client=None, cloud=None, registration=None, wait=None):
        self.root, self.store, self.batch_id = Path(root).resolve(), store, batch_id
        self.settings = MediaSettings(**config["settings"])
        self.config = config
        self.registration = registration or MediaRegistration(root, os.getenv("DATABASE_URL", ""))
        if self.registration.target != config["target"]:
            raise ValueError("실행 당시와 DB 대상이 다릅니다. 기존 작업을 다른 DB에 자동 등록하지 않습니다.")
        self.client = client or GoogleMediaClient(self.settings)
        self.cloud = cloud or S3MediaStore(self.settings.bucket, self.settings.base_url, region=self.settings.region)
        self.requests = RequestRunner(self.settings, stop=stop, wait=wait)
        saved = self.store.settings()
        self.requests.available = {kind: float(saved.get(f"media-available-{kind}", 0)) for kind in ("tts", "image")}
        self.directory = self.root / "data/media_generation"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _local(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("생성 파일 경로가 올바르지 않습니다.")
        return path

    def _artifact(self, path, **fields):
        size, sha = digest_file(path)
        return {"local_path": path.relative_to(self.root).as_posix(), "byte_size": size, "file_sha256": sha, **fields}

    def _valid(self, result):
        try:
            return digest_file(self._local(result["local_path"])) == (result["byte_size"], result["file_sha256"])
        except (OSError, KeyError, ValueError):
            return False

    def _perform(self, kind, snapshot, operation, *, artifact=False, network=True):
        key = _hash({"kind": kind, "snapshot": snapshot})
        task = self.store.media_task(self.batch_id, key, kind, snapshot)
        result = json.loads(task["result_json"])
        if artifact and self._valid(result):
            self.store.update_media(self.batch_id, key, status="succeeded", error="", next_attempt_at=0)
            return result
        if self.requests.stop.is_set():
            raise Stopped()

        def checkpoint(**fields):
            self.store.update_media(self.batch_id, key, **fields)
            self.store.pipeline(self.batch_id, kind, next_attempt_at=fields.get("next_attempt_at", 0))

        previous = self.requests.checkpoint
        self.requests.checkpoint = checkpoint
        try:
            if network:
                attempts = task["attempts"] if task["status"] in ("processing", "retry_wait", "failed") else 0
                result = self.requests.call(kind, lambda: operation(key), attempts=attempts, retry_at=task["next_attempt_at"])
            else:
                checkpoint(status="processing", attempts=task["attempts"] + 1, next_attempt_at=0)
                result = operation(key)
            self.store.update_media(self.batch_id, key, status="succeeded", result_json=_json(result), error="", next_attempt_at=0)
            return result
        except Stopped:
            self.store.update_media(self.batch_id, key, status="pending", error="사용자가 중단했습니다.")
            raise
        except Exception:
            # Provider response bodies and credentials must never enter job logs.
            self.store.update_media(self.batch_id, key, status="failed", error=f"{kind} 단계 실패 · 설정과 입력을 확인하세요.", next_attempt_at=0)
            raise
        finally:
            if kind in ("tts", "image"):
                self.store.set_setting(f"media-available-{kind}", str(self.requests.available.get(kind, 0)))
            self.requests.checkpoint = previous

    def _speech(self, speaker, text):
        s = self.settings
        snapshot = {"speaker": speaker, "text": text, "model": s.tts_model, "female": s.female, "male": s.male,
                    "rate": s.speaking_rate, "style": s.style_prompt, "version": "literal_v1"}

        def generate(key):
            data = self.client.speech(speaker, text)
            pcm(data)
            path = self.directory / f"{key}.wav"
            temporary = path.with_suffix(".part")
            temporary.write_bytes(data)
            temporary.replace(path)
            return self._artifact(path)
        result = self._perform("tts", snapshot, generate, artifact=True)
        return self._local(result["local_path"]).read_bytes()

    def _audio(self, task):
        s = self.settings
        snapshot = {"script": task["script"], "model": s.tts_model, "female": s.female, "male": s.male,
                    "rate": s.speaking_rate, "style": s.style_prompt, "composer": COMPOSER_VERSION}

        def generate(key):
            path = self.directory / f"{key}.mp3"
            temporary = self.directory / f"{key}.part.mp3"
            duration = compose(task["script"], self._speech, temporary, s)
            temporary.replace(path)
            return self._artifact(path, duration_ms=duration, mime_type="audio/mpeg")
        result = self._perform("compose", snapshot, generate, artifact=True, network=False)
        validate_mp3(self._local(result["local_path"]), s)
        row = self._upload(result, "generated-audio")
        generation_hash = _hash(snapshot)
        source_hash = _hash([generation_hash, result["file_sha256"]])
        return {**row, "kind": "audio", "audio_asset_id": str(uuid.uuid5(PUBLICATION_NAMESPACE, "audio:" + source_hash)),
                "source_hash": source_hash, "binding_source_hash": generation_hash, "positions": task["positions"],
                "model_name": s.tts_model, "female_voice": s.female, "male_voice": s.male,
                "narration_version": "exam_track_v4", "script_snapshot": {**task["script"], "generationSourceHash": generation_hash},
                "tts_style": {"speakingRate": s.speaking_rate, "stylePrompt": s.style_prompt}, "duration_ms": result["duration_ms"]}

    def _image(self, task):
        snapshot = {"prompt": task["prompt"], "chart_spec": task["chart_spec"], "model": self.settings.image_model,
                    "version": "exam_image_v1"}

        def generate(key):
            data = (render_chart(ChartSpec.model_validate(task["chart_spec"])) if task["chart_spec"] else self.client.image(task["prompt"]))
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                image = image.convert("RGB")
                image.thumbnail((1200, 900))
                path = self.directory / f"{key}.png"
                temporary = path.with_suffix(".part")
                image.save(temporary, format="PNG", optimize=True)
            temporary.replace(path)
            return self._artifact(path, mime_type="image/png")
        result = self._perform("chart" if task["chart_spec"] else "image", snapshot, generate,
                               artifact=True, network=not bool(task["chart_spec"]))
        row = self._upload(result, "generated-images")
        identity = {k: task[k] for k in ("item_id", "content_hash", "visual_role", "option_number")}
        return {**row, **identity, "kind": "image",
                "visual_asset_id": str(uuid.uuid5(PUBLICATION_NAMESPACE, "image:" + _hash([identity, row["file_sha256"]])))}

    def _upload(self, artifact, namespace):
        path = self._local(artifact["local_path"])
        key = destination_key(namespace, artifact["file_sha256"], artifact["mime_type"])
        row = {k: artifact[k] for k in ("byte_size", "file_sha256", "mime_type")}
        row.update(storage_backend="s3", storage_bucket=self.cloud.bucket, storage_path=key,
                   storage_url=cloudfront_url(self.settings.base_url, key))

        def upload(_):
            self.cloud.upload(path, key, row["byte_size"], row["file_sha256"], row["mime_type"])
            return row
        self._perform("upload", row, upload)
        self._perform("verify", row, lambda _: self._verify(row, path))
        return row

    def _verify(self, row, path=None):
        self.cloud.verify(row, local_path=path)
        return row

    def run(self):
        try:
            self.registration.preflight()
            package = self.registration.package(self.batch_id)
            plan = media_plan(package, self.settings)
            with self.store.connect() as c:
                c.execute("UPDATE media_tasks SET active=0 WHERE batch_id=?", (self.batch_id,))
            assets, failures = [], []
            for task in plan:
                try:
                    assets.append(self._audio(task) if task["kind"] == "audio" else self._image(task))
                except Stopped:
                    raise
                except Exception:
                    failures.append(task)
            if failures:
                raise ValueError(f"필수 미디어 {len(failures)}건을 완료하지 못했습니다. 성공분은 보존했습니다.")
            if self.requests.stop.is_set():
                raise Stopped()
            # Recheck the complete delivery manifest immediately before registration.
            for row in assets:
                self._perform("verify", row, lambda _, row=row: self._verify(row))
            package["media"] = assets
            self._perform("register", package, lambda _: {"job_id": self.registration.register(package)})
            self.store.pipeline(self.batch_id, "succeeded", auto_resume=0, next_attempt_at=0)
        finally:
            self.client.close()
            self.cloud.close()
