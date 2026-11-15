"""Generate and retain local 53rd-question images using the shared media client."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from topik_question_lab.common.media.media_provider import MediaClient, MediaSettings
from topik_question_lab.common.paths import application_root
from topik_question_lab.common.writing_models import WritingQuestion
from topik_question_lab.menus.writing.writing_prompts import DEFAULT_IMAGE_STYLE, build_image_prompt, prompt_settings


IMAGE_ERROR = "자료 이미지 생성에 실패했습니다. 이미지 서비스·모델 설정을 확인한 뒤 다시 시도하세요."


def image_path(question: WritingQuestion, directory: Path | None = None) -> Path:
    # A saved edit must never reuse an image containing the previous data.
    digest = hashlib.sha256(question.model_dump_json().encode()).hexdigest()
    directory = directory if directory is not None else application_root() / "data/media/writing"
    return directory / f"{digest}.png"


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
