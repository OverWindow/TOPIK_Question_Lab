from topik_question_lab.common.ui.navigation import menu_page
import base64
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from test_ibt_pipeline import COMBO, generated_batch
from test_media_generation import Clock, FakeGoogle, assert_image_color, pipeline, seed_media, settings
from topik_question_lab.common.listening_models import ChartSpec
from topik_question_lab.common.media import media_generation as media
from topik_question_lab.common.media.media_provider import GoogleMediaClient, MediaError
from topik_question_lab.common.question_bank.direct_publication import expected_item, package_draft
from topik_question_lab.common.question_bank.publication import collect_candidate_catalog
from topik_question_lab.common.question_bank.storage_overview import preparation_progress


def test_local_chart_preview_defaults_to_grayscale_and_can_preserve_color():
    spec = ChartSpec.model_validate(COMBO)
    assert_image_color(media.render_chart(spec), color=False)
    assert_image_color(media.render_chart(spec, color=True), color=True)


def test_chart_request_keeps_endpoint_and_body_shape_without_scene_text_prohibition():
    class Credentials:
        valid = True
        token = "synthetic-secret"

    requests = []
    png = FakeGoogle().image("test")

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode()}}
        ]}}]})

    client = GoogleMediaClient(settings(), credentials=Credentials(),
                               client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        prompt = media.chart_image_prompt(COMBO)
        assert client.image(prompt, chart=True) == png
        assert client.image("학생이 책을 읽는 장면") == png
        assert client.image("학생이 책을 읽는 흑백 삽화", color=True) == png
    finally:
        client.close()
    chart, scene, color_scene = [json.loads(r.content) for r in requests]
    assert requests[0].method == requests[1].method == "POST"
    assert requests[0].url == requests[1].url == requests[2].url
    assert chart["generationConfig"] == scene["generationConfig"] == color_scene["generationConfig"]
    assert chart["contents"][0]["parts"] == [{"text": prompt}]
    assert json.loads(prompt.split("\n", 1)[1]) == COMBO
    assert "no text, labels, numbers" not in prompt
    assert "no text, labels, numbers" in scene["contents"][0]["parts"][0]["text"]
    assert "Black-and-white line art" in scene["contents"][0]["parts"][0]["text"]
    color_prompt = color_scene["contents"][0]["parts"][0]["text"]
    assert "Full-color illustration" in color_prompt
    assert "no text, labels, numbers" in color_prompt
    assert color_prompt.endswith("Use full color even if the scene description requests monochrome styling.")
    assert "full-color palette" in prompt


@pytest.mark.parametrize("exam_format", ["pbt", "ibt"])
@pytest.mark.parametrize("cached_style", ["legacy", "opposite"])
def test_scene_color_cache_and_progress_follow_exam_format(tmp_path, monkeypatch, exam_format, cached_style):
    if exam_format == "ibt":
        _, bid, _, _ = generated_batch(tmp_path, "listening", monkeypatch)
    else:
        bid = seed_media(tmp_path, "listening")
    run = pipeline(tmp_path, bid)
    task = next(t for t in media.media_plan(run.registration.package(bid), run.settings)
                if t["kind"] == "image" and not t["chart_spec"])
    color = exam_format == "ibt"
    assert task["color"] == color
    snapshot = {"prompt": task["prompt"], "chart_spec": None, "model": run.settings.image_model,
                "version": "exam_image_v1"}
    if cached_style == "opposite":
        snapshot["color"] = not color
    old_data = media.render_image({**task, "color": not color}, FakeGoogle())

    def old_image(key):
        path = run.directory / f"{key}.png"
        path.write_bytes(old_data)
        return run._artifact(path, mime_type="image/png")

    old = run._perform("image", snapshot, old_image, artifact=True, network=False)
    run._upload(old, "generated-images")
    batch = run.store.batches()[0]
    records = collect_candidate_catalog(tmp_path).records
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["images_ready"] == 0
    asset = run._image(task)
    assert run.client.image_colors == [color]
    assert_image_color(run.cloud.objects[asset["storage_path"]][0], color=color)
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["images_ready"] > 0
    assert run._image(task) == asset
    assert run.client.image_colors == [color]
    assert run._local(old["local_path"]).read_bytes() == old_data


@pytest.mark.parametrize("section,chart_count", [("reading", 1), ("listening", 4)])
def test_ibt_chart_images_preserve_items_and_ignore_legacy_render_cache(tmp_path, monkeypatch, section, chart_count):
    runner, bid, _, _ = generated_batch(tmp_path, section, monkeypatch)
    run = pipeline(tmp_path, bid)
    package = run.registration.package(bid)
    original = deepcopy(package)
    expected = [expected_item(i) for i in package_draft(package).items]
    plan = media.media_plan(package, run.settings)
    charts = [t for t in plan if t.get("model_chart")]
    assert len(charts) == chart_count
    assert all(t.get("model_chart") for t in plan if t.get("chart_spec"))
    for task in charts:
        assert json.loads(task["prompt"].split("\n", 1)[1]) == task["chart_spec"]

    # A successful old renderer checkpoint must not satisfy the new model plan.
    old = {k: v for k, v in charts[0].items() if k != "model_chart"}
    candidate = next(i.candidate for i in package_draft(package).items if str(i.candidate.item_id) == old["item_id"])
    old["prompt"] = candidate.question["visual_options"][0]["image_prompt"] if section == "listening" else ""
    old_asset = run._image(old)
    records = collect_candidate_catalog(tmp_path).records
    batch = runner.store.batches()[0]
    assert preparation_progress(batch, records, run.store.media_tasks(bid))["images_ready"] == 0

    def no_render(*args):
        pytest.fail("IBT charts must use the image model")

    monkeypatch.setattr(media, "render_chart", no_render)
    assets = [run._image(t) for t in plan if t["kind"] == "image"]
    for asset in assets:
        assert_image_color(run.cloud.objects[asset["storage_path"]][0], color=True)
    assert set(run.client.chart_prompts) == {t["prompt"] for t in charts}
    assert len(run.client.images) == len(set(t["prompt"] for t in plan if t["kind"] == "image"))
    for task in charts:
        asset = next(a for a in assets if a["item_id"] == task["item_id"] and a["option_number"] == task["option_number"])
        assert asset["content_hash"] == task["content_hash"]
        assert asset["visual_role"] == task["visual_role"]
        assert asset["mime_type"] == "image/png" and asset["storage_backend"] == "s3"
        assert set(asset) == set(old_asset)
    tasks = run.store.media_tasks(bid)
    assert any(t["kind"] == "image" and json.loads(t["snapshot_json"])["version"] == "ibt_chart_image_v1" for t in tasks)
    assert preparation_progress(batch, records, tasks)["images_ready"] == len(assets)
    calls = list(run.client.images)
    assert [run._image(t) for t in plan if t["kind"] == "image"] == assets
    assert run.client.images == calls
    assert package == original == run.registration.package(bid)
    assert [expected_item(i) for i in package_draft(package).items] == expected


def test_ibt_chart_retry_pacing_and_upload_resume_reuse_model_image(tmp_path, monkeypatch):
    _, bid, _, _ = generated_batch(tmp_path, "reading", monkeypatch)
    run = pipeline(tmp_path, bid, retries=1)
    chart = next(t for t in media.media_plan(run.registration.package(bid), run.settings) if t.get("model_chart"))
    clock = Clock()
    run.requests.now, run.requests.wait = clock.now, clock.wait
    generate = run.client.image
    attempts = []

    def limited(prompt, *, chart=False):
        attempts.append(clock.now())
        if len(attempts) == 1:
            raise MediaError("429", retryable=True, retry_after=60)
        return generate(prompt, chart=chart)

    monkeypatch.setattr(run.client, "image", limited)
    run.cloud.fail = True
    with pytest.raises(MediaError):
        run._image(chart)
    assert len(attempts) == 2 and attempts[1] - attempts[0] >= 60
    assert not run.registration.registered
    run.cloud.fail = False
    run.store.reset_media_attempts(bid)
    asset = run._image(chart)
    assert len(attempts) == 2
    assert asset["mime_type"] == "image/png"
    assert any(t["kind"] == "image" and t["status"] == "succeeded" for t in run.store.media_tasks(bid))


@pytest.mark.parametrize("section", ["reading", "listening"])
def test_pbt_charts_still_render_locally(tmp_path, section):
    bid = seed_media(tmp_path, section)
    run = pipeline(tmp_path, bid)
    charts = [t for t in media.media_plan(run.registration.package(bid), run.settings) if t.get("chart_spec")]
    assert charts and all(not t.get("model_chart") for t in charts)
    for task in charts:
        asset = run._image(task)
        assert asset["mime_type"] == "image/png"
        assert_image_color(run.cloud.objects[asset["storage_path"]][0], color=False)
    assert not run.client.images


def test_ibt_reading_registration_waits_for_model_image_and_preserves_package(tmp_path, monkeypatch):
    _, bid, _, _ = generated_batch(tmp_path, "reading", monkeypatch)
    run = pipeline(tmp_path, bid)
    before = deepcopy(run.registration.package(bid))
    image = run.client.image

    def denied(*args, **kwargs):
        raise MediaError("403")

    monkeypatch.setattr(run.client, "image", denied)
    with pytest.raises(ValueError, match="필수 미디어 4건"):
        run.run()
    assert not run.registration.registered
    assert run.registration.package(bid) == before
    monkeypatch.setattr(run.client, "image", image)
    run.store.reset_media_attempts(bid)
    run.run()
    registered = deepcopy(run.registration.registered[0])
    assets = registered.pop("media")
    assert registered == before and len(assets) == 4
    assert len(run.client.chart_prompts) == 1
    assert run.store.batches()[0]["pipeline_status"] == "succeeded"


@pytest.mark.parametrize("section,type_id,save_label", [
    ("reading", "ibt_chart_content_match", "문제 수정 저장"),
    ("listening", "ibt_visual_chart", "수정·평가 저장하고 다음 문항"),
])
def test_ibt_review_shows_data_without_rendering_or_changing_chart_content(tmp_path, monkeypatch, section, type_id, save_label):
    from streamlit.testing.v1 import AppTest
    from topik_question_lab.common.generation.bulk_generation import storage_for

    generated_batch(tmp_path, section, monkeypatch)
    storage = storage_for(tmp_path, section, type_id)
    record = storage.list_generated()[0]
    before = deepcopy(record["question"])
    if section == "reading":
        # Seed a complete stem; the editor already rebuilds it on save.
        before["stem"] = "\n".join(before[k] for k in ("auxiliary_text", "passage", "question_prompt") if before[k])
        storage.save_generated_edit(record["id"], before)

    def no_render(*args):
        pytest.fail("IBT review must not render or attach a local chart")

    monkeypatch.setattr("topik_question_lab.common.media.listening_visuals.render_chart", no_render)
    page = "app.py" if section == "reading" else "listening_app.py"
    app = AppTest.from_file(str(menu_page(page)), default_timeout=30).run()
    app.sidebar.radio(key=f"{section}-exam-format").set_value("ibt").run()
    app.sidebar.selectbox[0].set_value(type_id).run()
    app.sidebar.radio(key=f"{section}-work-stage").set_value("5. 검수·비교").run()
    assert not app.exception
    assert any("그래프 원본 수치" in c.value for c in app.caption)
    next(b for b in app.button if b.label == save_label).click().run()
    assert not app.exception
    after = storage.list_generated()[0]["question"]
    assert after == before
    assert not list((tmp_path / "data").rglob("*.png"))
