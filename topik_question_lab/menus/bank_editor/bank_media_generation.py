"""Generate replacement previews without writing to PostgreSQL or S3."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from topik_question_lab.common.media.audio_composer import compose
from topik_question_lab.common.media.media_generation import image_tasks, render_image
from topik_question_lab.common.media.media_provider import MediaClient, MediaSettings, RequestRunner
from topik_question_lab.common.question_bank.bank_editor import prepare_edit, replacement_signature
from topik_question_lab.common.question_bank.direct_publication import _hash, PublicationBlocked


def generate_replacements(plan, *, settings=None, client=None, progress=None):
    plan = prepare_edit(plan["snapshot"], plan["question"], plan["metadata"], plan["replacements"], allow_generation=True)
    pending = [r for r in plan["replacements"] if r.get("generate")]
    if not pending:
        return plan
    row, question = plan["snapshot"]["item"], plan["question"]
    candidate = SimpleNamespace(section=row["section"], question_type=row["item_type"], slot=row["type_slot"],
                                exam_format=question.get("exam_format", "pbt"), item_id=row["item_id"], question=question)
    targets = {(r["visual_role"], r["option_number"]) for r in pending if r["kind"] == "image"}
    images = {(t["visual_role"], t["option_number"]): t for t in image_tasks(candidate, row["content_hash"], targets)}
    if set(images) != targets:
        raise PublicationBlocked("자동 생성할 이미지의 설명·도표 데이터를 확인하세요.")
    settings = settings or MediaSettings.from_env()
    audio = any(r["kind"] == "audio" for r in pending)
    remote_images = any(t.get("model_chart") or t.get("model_banner") or not (t.get("banner_text") or t["chart_spec"]) for t in images.values())
    remote = audio or remote_images
    if remote:
        settings.validate("listening" if audio else "reading", images=remote_images, delivery=False)
    owned_client = client is None and remote
    client = MediaClient(settings) if owned_client else client
    requests = RequestRunner(settings)
    speech_cache = {}

    def speech(speaker, text):
        key = (speaker, text)
        if key not in speech_cache:
            speech_cache[key] = requests.call("tts", lambda: client.speech(speaker, text))
        return speech_cache[key]

    replacements = []
    try:
        if owned_client:
            client.authenticate("listening" if audio else "reading", images=remote_images)
        for replacement in plan["replacements"]:
            if not replacement.get("generate"):
                replacements.append(replacement)
                continue
            kind = replacement["kind"]
            if progress:
                progress("음원 생성 중" if kind == "audio" else "이미지 생성 중")
            signature = replacement_signature(plan, replacement)
            generation = {"input_hash": _hash(signature)}
            if kind == "audio":
                with TemporaryDirectory(prefix="topik-bank-generate-") as directory:
                    path = Path(directory) / "preview.mp3"
                    compose(signature, speech, path, settings)
                    data = path.read_bytes()
                generation.update(provider="google_cloud_tts", model_name=settings.tts_model,
                                  female_voice=settings.female, male_voice=settings.male,
                                  tts_style={"speakingRate": settings.speaking_rate, "stylePrompt": settings.style_prompt})
            else:
                task = images[(replacement["visual_role"], replacement["option_number"])]
                local = bool(task.get("banner_text") or task["chart_spec"]) and not (task.get("model_chart") or task.get("model_banner"))
                data = render_image(task, client) if local else requests.call("image", lambda: render_image(task, client))
                if not local:
                    generation.update(provider=settings.image_provider, model_name=settings.image_model)
            replacements.append({k: v for k, v in replacement.items() if k != "generate"} | {"data": data, "generation": generation})
    except Exception:
        raise PublicationBlocked("미디어 자동 생성에 실패했습니다. 생성 설정과 입력 내용을 확인한 뒤 다시 시도하세요. 기존 미디어는 변경하지 않았습니다.") from None
    finally:
        if owned_client:
            client.close()
    return prepare_edit(plan["snapshot"], question, plan["metadata"], replacements)
