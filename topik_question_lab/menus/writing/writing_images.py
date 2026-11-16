"""Generate and retain local 53rd-question images using the shared media client."""
from __future__ import annotations

import io
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from topik_question_lab.common.media.media_provider import MediaClient, MediaSettings
from topik_question_lab.common.media.media_cloud import S3MediaStore, cloudfront_url, destination_key, digest_file
from topik_question_lab.common.paths import application_root
from topik_question_lab.common.writing_models import WritingImage, WritingQuestion
from topik_question_lab.menus.writing.writing_prompts import DEFAULT_IMAGE_STYLE, build_image_prompt, prompt_settings


IMAGE_ERROR = "자료 이미지 생성에 실패했습니다. 이미지 서비스·모델 설정을 확인한 뒤 다시 시도하세요."


def image_path(question: WritingQuestion, directory: Path | None = None) -> Path:
    # A saved edit must never reuse an image containing the previous data.
    digest = question.content_sha256()
    directory = directory if directory is not None else application_root() / "data/media/writing"
    return directory / f"{digest}.png"


def upload_image(question: WritingQuestion, *, directory: Path | None = None) -> WritingImage:
    if question.type_slot != 53:
        raise ValueError("자료 이미지는 쓰기 53번에만 저장할 수 있습니다.")
    path = image_path(question, directory)
    if not path.is_file():
        raise ValueError("현재 53번 문항의 자료 이미지가 없습니다. ‘5. 수정·내보내기’에서 이미지를 생성한 뒤 저장하세요.")
    store = None
    try:
        with Image.open(path) as source:
            if source.format != "PNG":
                raise ValueError("PNG required")
            source.load()
        size, digest = digest_file(path)
        store = S3MediaStore(os.getenv("S3_BUCKET", ""), os.getenv("CLOUDFRONT_BASE_URL", ""))
        key = destination_key("writing", digest, "image/png")
        asset = WritingImage(storage_bucket=store.bucket, storage_path=key,
                             storage_url=cloudfront_url(store.base_url, key), byte_size=size,
                             file_sha256=digest, question_sha256=question.content_sha256())
        store.upload(path, key, size, digest, "image/png")
        store.verify(asset.model_dump(), local_path=path)
        return asset
    except Exception:
        raise ValueError("53번 이미지 S3 업로드·CloudFront 검증에 실패했습니다. 설정과 파일을 확인한 뒤 다시 저장하세요.") from None
    finally:
        if store is not None:
            store.close()


def generate_image(question: WritingQuestion, *, directory: Path | None = None,
                   style: str = DEFAULT_IMAGE_STYLE, settings: MediaSettings | None = None) -> Path:
    prompt = build_image_prompt(question, style)
    client = None
    temporary = None
    try:
        settings = settings or MediaSettings.from_env()
        settings.validate("writing", images=True, delivery=False)
        client = MediaClient(settings)
        client.authenticate("writing")
        data = client.image(prompt, chart=True)
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            canvas = Image.new("RGBA", source.size, "white")
            canvas.alpha_composite(source.convert("RGBA"))
            rendered = canvas.convert("L").convert("RGB")
        metadata = PngInfo()
        metadata.add_text("prompt", prompt)
        metadata.add_text("provider", settings.image_provider)
        metadata.add_text("model", settings.image_model)
        path = image_path(question, directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as output:
            temporary = Path(output.name)
            rendered.save(output, format="PNG", pnginfo=metadata)
        # Keep the previous image intact if generation or encoding fails.
        temporary.replace(path)
        return path
    except Exception:
        raise ValueError(IMAGE_ERROR) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if client is not None:
            client.close()


def generate_result_images(storage, receipt: dict) -> dict:
    if receipt.get("error") or not receipt.get("question_ids"):
        return receipt
    try:
        style = prompt_settings(storage, 53)["image_style"]
        for row in storage.list_generated(receipt["run_id"]):
            if row["id"] in receipt["question_ids"] and row["question"]["type_slot"] == 53:
                generate_image(WritingQuestion.model_validate(row["question"]), style=style,
                               directory=storage.path.parent / "media/writing")
    except Exception:
        return {**receipt, "image_error": IMAGE_ERROR}
    return receipt
