"""Verified S3/CloudFront media delivery."""
from __future__ import annotations

import base64
import hashlib
import os
from contextlib import closing
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from topik_question_lab.common.media.media_validation import LocalMediaValidator, preview_media

EXTENSIONS = {"audio/mpeg": "mp3", "image/png": "png", "image/webp": "webp", "image/svg+xml": "svg"}
CACHE_CONTROL = "public, max-age=31536000, immutable"


class MediaCloudError(ValueError):
    """A credential-safe operational error suitable for the CLI and UI."""

    def __init__(self, message, *, retryable=False, retry_after=0):
        super().__init__(message)
        self.retryable, self.retry_after = retryable, retry_after


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise MediaCloudError("미디어 주소는 경로·인증정보 없는 HTTPS origin이어야 합니다.")
    return value.rstrip("/")


def cloudfront_url(base_url: str, path: str) -> str:
    return https_origin(base_url) + "/" + quote(path, safe="/")


def content_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def destination_key(bucket: str, sha256: str, mime_type: str) -> str:
    if mime_type not in EXTENSIONS:
        raise MediaCloudError("지원하지 않는 미디어 MIME 유형입니다.")
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise MediaCloudError("유효한 파일 SHA-256이 필요합니다.")
    if not bucket or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in bucket):
        raise MediaCloudError("원본 버킷명이 올바르지 않습니다.")
    return f"media/{bucket}/{sha256}.{EXTENSIONS[mime_type]}"


def digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def aws_session(region: str | None = None):
    try:
        import boto3
    except ImportError as exc:
        raise MediaCloudError("boto3가 없습니다. requirements.txt를 설치하세요.") from exc
    if os.environ.get("AWS_PROFILE") == "":
        os.environ.pop("AWS_PROFILE")
    return boto3.Session(profile_name=os.getenv("AWS_PROFILE") or None,
                         region_name=region or os.getenv("AWS_REGION") or "ap-northeast-2")


class S3MediaStore:
    def __init__(self, bucket: str, base_url: str, *, region=None, s3=None, client=None):
        if not bucket.strip():
            raise MediaCloudError("S3_BUCKET을 설정하세요.")
        self.bucket, self.base_url = bucket, https_origin(base_url)
        if s3 is None:
            from botocore.config import Config
            s3 = aws_session(region).client("s3", config=Config(retries={"total_max_attempts": 1}, connect_timeout=15, read_timeout=120))
        self.s3 = s3
        self.client = client or httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=False)

    def _verify_s3(self, key, size, sha256, mime_type):
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        with closing(response["Body"]) as stream:
            digest, received = hashlib.sha256(), 0
            if response["ContentLength"] != size or content_type(response.get("ContentType", "")) != mime_type:
                raise MediaCloudError("S3 파일 크기·MIME 유형이 원본과 다릅니다.")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                received += len(chunk)
            if received != size or digest.hexdigest() != sha256:
                raise MediaCloudError("S3 파일 SHA-256이 원본과 다릅니다.")

    def upload(self, path: Path, key: str, size: int, sha256: str, mime_type: str):
        # A hash-shaped name alone is not evidence that the bytes are correct.
        if digest_file(path) != (size, sha256):
            raise MediaCloudError("로컬 원본의 크기·해시가 변경되었습니다.")
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            self._verify_s3(key, size, sha256, mime_type)
            return
        with path.open("rb") as stream:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=stream, ContentLength=size,
                               ContentType=mime_type, CacheControl=CACHE_CONTROL,
                               ServerSideEncryption="AES256", IfNoneMatch="*",
                               ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode("ascii"),
                               Metadata={"file-sha256": sha256})
        self._verify_s3(key, size, sha256, mime_type)

    def verify(self, row, *, local_path: Path | None = None):
        if row["storage_bucket"] != self.bucket:
            raise MediaCloudError("검증 대상이 설정된 S3 버킷과 다릅니다.")
        key, size, sha256, mime = (row[k] for k in ("storage_path", "byte_size", "file_sha256", "mime_type"))
        url = cloudfront_url(self.base_url, key)
        if row["storage_url"] != url:
            raise MediaCloudError("DB의 CloudFront 주소가 객체 경로와 다릅니다.")
        if local_path is not None and digest_file(local_path) != (size, sha256):
            raise MediaCloudError("보관한 원본 파일의 크기·해시가 다릅니다.")
        self._verify_s3(key, size, sha256, mime)
        try:
            with self.client.stream("GET", url) as response:
                if response.status_code != 200:
                    from topik_question_lab.common.media.media_provider import retry_after_seconds
                    raise MediaCloudError(f"CloudFront 파일 응답 오류 (HTTP {response.status_code})",
                        retryable=response.status_code in (404, 408, 429, 500, 502, 503, 504),
                        retry_after=retry_after_seconds(response.headers.get("Retry-After")))
                if response.status_code != 200 or content_type(response.headers.get("content-type", "")) != mime:
                    raise MediaCloudError("CloudFront 파일 응답·MIME 유형이 올바르지 않습니다.")
                digest, received = hashlib.sha256(), 0
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    received += len(chunk)
                if received != size or digest.hexdigest() != sha256:
                    raise MediaCloudError("CloudFront 파일 크기·SHA-256이 원본과 다릅니다.")
            if mime.startswith("audio/"):
                with self.client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                    if response.status_code in (404, 408, 429, 500, 502, 503, 504):
                        raise MediaCloudError("CloudFront 음성 부분 요청을 재시도합니다.", retryable=True)
                    if (response.status_code != 206 or response.headers.get("content-range") != f"bytes 0-0/{size}"
                            or len(response.read()) != 1):
                        raise MediaCloudError("CloudFront 음성 부분 요청 검증에 실패했습니다.")
        except httpx.HTTPError as exc:
            raise MediaCloudError("CloudFront 연결에 실패했습니다.") from exc

    def close(self):
        self.client.close()


class S3MediaValidator(LocalMediaValidator):
    def __init__(self, store: S3MediaStore):
        self.store = store

    def _validate_storage(self, row):
        if row["storage_backend"] != "s3" or not row["file_sha256"]:
            raise MediaCloudError("S3에 저장되고 SHA-256이 기록된 미디어만 검사할 수 있습니다.")

    def _read(self, row):
        self.store.verify(row)


def preview_s3_media(database_url, set_ids, *, store=None):
    owned = store is None
    try:
        store = store or S3MediaStore(os.getenv("S3_BUCKET", ""), os.getenv("CLOUDFRONT_BASE_URL", ""))
        return preview_media(database_url, set_ids, S3MediaValidator(store))
    except ValueError:
        raise
    except Exception as exc:
        raise MediaCloudError("S3 검증에 실패했습니다. AWS 인증·권한·버킷 설정을 확인하세요.") from exc
    finally:
        if owned and store is not None:
            store.close()
