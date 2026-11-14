import base64
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from test_bank_media_generation import snapshot, plan
from test_bizrouter_images import image_response, mock_images
from test_ibt_pipeline import generated_batch
from test_media_generation import Clock, FakeGoogle, assert_image_color, pipeline, settings
from topik_question_lab.common.generation.bulk_generation import storage_for
from topik_question_lab.common.media import media_generation as media
from topik_question_lab.common.media.media_provider import GoogleMediaClient, MediaError
from topik_question_lab.common.question_bank.bank_editor import prepare_edit
from topik_question_lab.common.question_bank.direct_publication import PublicationBlocked, package_draft
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog
from topik_question_lab.common.question_bank.storage_overview import preparation_progress
from topik_question_lab.common.ui.navigation import menu_page
from topik_question_lab.menus.bank_editor.bank_media_generation import generate_replacements


COPY = '하루 한 시간,  함께 걸어요!\n“건강한 내일”을 준비하세요. 11월 14일 (토)'


@pytest.mark.parametrize("provider", ["google", "bizrouter"])
def test_banner_request_preserves_exact_copy_and_allows_korean_typography(monkeypatch, provider):
    requests = []
    png = FakeGoogle().image("test")

    def respond(request):
        requests.append(request)
        if provider == "bizrouter":
            return httpx.Response(200, json=image_response())
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode()}}
        ]}}]})

    client = (mock_images(monkeypatch, respond) if provider == "bizrouter" else
              GoogleMediaClient(settings(), credentials=SimpleNamespace(valid=True, token="synthetic"),
                                client=httpx.Client(transport=httpx.MockTransport(respond))))
    prompt = media.banner_image_prompt(COPY)
    try:
        assert client.image(prompt, banner=True, color=True) == png
    finally:
        client.close()
    body = json.loads(requests[0].content)
    ratio = body["aspect_ratio"] if provider == "bizrouter" else body["generationConfig"]["imageConfig"]["aspectRatio"]
    assert ratio == "4:1"
    sent = body["messages"][0]["content"] if provider == "bizrouter" else body["contents"][0]["parts"][0]["text"]
    assert sent == prompt
    assert json.loads(sent.split("\n", 1)[1]) == {"text": COPY}
    assert "no text, labels, numbers" not in sent
    assert "advertising banner" in sent and "thin border" in sent


@pytest.mark.parametrize("provider", ["google", "bizrouter"])
def test_banner_pipeline_replaces_legacy_cache_and_preserves_question(tmp_path, monkeypatch, provider):
    _, bid, _, _ = generated_batch(tmp_path, "reading", monkeypatch)
    run = pipeline(tmp_path, bid)
    run.settings = replace(run.settings, image_provider=provider)
    run.store.configure_media(bid, {**run.config, "settings": run.settings.snapshot()})
    original = deepcopy(run.registration.package(bid))
    banners = [t for t in media.media_plan(original, run.settings) if t.get("model_banner")]
    assert len(banners) == 3
    candidates = {str(i.candidate.item_id): i.candidate for i in package_draft(original).items}
    for task in banners:
        copy = candidates[task["item_id"]].question["passage"]
        assert task["banner_text"] == copy
        assert json.loads(task["prompt"].split("\n", 1)[1]) == {"text": copy}

    legacy = {k: v for k, v in banners[0].items() if k != "model_banner"}
    legacy["prompt"] = ""
    old_asset = run._image(legacy)
    records = collect_candidate_catalog(tmp_path).records
    batch = run.store.batches()[0]
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["images_ready"] == 0

    def no_local_banner(*args):
        pytest.fail("IBT banners must use the image model")

    monkeypatch.setattr(media, "render_banner", no_local_banner)
    assets = [run._image(t) for t in banners]
    assert run.client.banner_prompts == [t["prompt"] for t in banners]
    assert run.client.image_colors == [True] * 3
    for task, asset in zip(banners, assets):
        assert asset["item_id"] == task["item_id"] and asset["content_hash"] == task["content_hash"]
        assert asset["visual_role"] == "material" and asset["option_number"] == 1
        assert set(asset) == set(old_asset)
        assert_image_color(run.cloud.objects[asset["storage_path"]][0], color=True)
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["images_ready"] == 3
    snapshots = [json.loads(t["snapshot_json"]) for t in run.store.media_tasks(bid) if t["kind"] == "image"]
    assert all(s["version"] == "ibt_banner_image_v1" for s in snapshots)
    assert all(s.get("provider", "google") == provider for s in snapshots)
    if provider == "bizrouter":
        assert all(s["aspect_ratio"] == "4:1" for s in snapshots)
    assert [run._image(t) for t in banners] == assets
    assert len(run.client.images) == 3
    assert run.registration.package(bid) == original


def test_banner_retry_and_upload_resume_do_not_regenerate_success(tmp_path, monkeypatch):
    _, bid, _, _ = generated_batch(tmp_path, "reading", monkeypatch)
    run = pipeline(tmp_path, bid, retries=1)
    banner = next(t for t in media.media_plan(run.registration.package(bid), run.settings) if t.get("model_banner"))
    clock = Clock()
    run.requests.now, run.requests.wait = clock.now, clock.wait
    generate = run.client.image
    attempts = []

    def limited(prompt, **kwargs):
        attempts.append(clock.now())
        if len(attempts) == 1:
            raise MediaError("429", retryable=True, retry_after=60)
        return generate(prompt, **kwargs)

    monkeypatch.setattr(run.client, "image", limited)
    run.cloud.fail = True
    with pytest.raises(MediaError):
        run._image(banner)
    assert len(attempts) == 2 and attempts[1] - attempts[0] >= 60
    run.cloud.fail = False
    run.store.reset_media_attempts(bid)
    assert run._image(banner)["mime_type"] == "image/png"
    assert len(attempts) == 2 and len(run.client.banner_prompts) == 1


def test_banner_replacement_keeps_copy_and_rejects_stale_image(snapshot):
    current = snapshot("reading", 4, "ibt")
    before = deepcopy(current)
    target = dict(kind="image", visual_role="material", option_number=1, generate=True)
    pending = plan(current, [target], passage=COPY, visual_material={"text": COPY})
    client = FakeGoogle()
    ready = generate_replacements(pending, settings=settings(image_delay=0), client=client)
    assert ready["question"]["passage"] == COPY
    assert ready["question"]["visual_material"] == {"text": COPY}
    assert client.banner_prompts == [media.banner_image_prompt(COPY)]
    assert ready["replacements"][0]["generation"]["model_name"] == settings().image_model
    assert current == before
    changed = deepcopy(ready["question"])
    changed.update(passage="함께 걸어요!", visual_material={"text": "함께 걸어요!"})
    with pytest.raises(PublicationBlocked, match="다시 생성"):
        prepare_edit(current, changed, ready["metadata"], ready["replacements"])


def test_banner_review_shows_original_copy_without_old_border_preview(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    generated_batch(tmp_path, "reading", monkeypatch)
    storage = storage_for(tmp_path, "reading", "ibt_short_text_topic")
    record = storage.list_generated()[0]
    before = deepcopy(record["question"])
    before["stem"] = "\n".join(before[k] for k in ("auxiliary_text", "passage", "question_prompt") if before[k])
    storage.save_generated_edit(record["id"], before)
    app = AppTest.from_file(str(menu_page("app.py")), default_timeout=30).run()
    app.sidebar.radio(key="reading-exam-format").set_value("ibt").run()
    app.sidebar.selectbox[0].set_value("ibt_short_text_topic").run()
    app.sidebar.radio(key="reading-work-stage").set_value("5. 검수·비교").run()
    assert not app.exception
    assert any("배너 원문" in c.value for c in app.caption)
    assert any(t.value == before["visual_material"]["text"] for t in app.text)
    assert not app.get("imgs")
    next(b for b in app.button if b.label == "문제 수정 저장").click().run()
    assert not app.exception
    assert storage.list_generated()[0]["question"] == before
