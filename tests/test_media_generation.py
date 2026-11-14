import io
import json
import shutil
import sqlite3
import threading

import httpx
from PIL import Image
import pytest

from test_direct_publication import seed_batch, change_question
from topik_question_lab.common.generation.bulk_generation import BulkRunner, BulkStore
from topik_question_lab.common.media.media_generation import MediaPipeline, chart_spec, media_plan
from topik_question_lab.common.media.media_provider import GoogleMediaClient, MediaError, MediaSettings, RequestRunner, Stopped
from topik_question_lab.common.question_bank.direct_publication import DirectPublicationService, PublicationBlocked
from topik_question_lab.common.media.media_cloud import digest_file
from topik_question_lab.common.media.media_registration import MediaRegistration, check_media
from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank


SPEC = {"chart_type": "bar", "title": "교통 조사", "labels": ["버스", "지하철"], "values": [60, 40], "unit": "%"}


def seed_media(root, section="reading"):
    BulkStore(root)
    bid = seed_batch(root, section, approved=False)
    if section == "reading":
        path = root / "data/types/content_match_short.db"
        with sqlite3.connect(path) as c:
            for qid, raw in c.execute("SELECT id,data_json FROM generated_questions").fetchall():
                question = json.loads(raw)
                if question["type_slot"] == 10:
                    question.update(passage="교통 조사 버스: 60%, 지하철: 40%", visual_material={"chart_spec": SPEC})
                    c.execute("UPDATE generated_questions SET edited_json=? WHERE id=?", (json.dumps(question), qid))
    else:
        for path in (root / "data/listening/types").glob("*.db"):
            with sqlite3.connect(path) as c:
                for qid, raw in c.execute("SELECT id,data_json FROM generated_questions").fetchall():
                    question = json.loads(raw)
                    if question["visual_kind"] == "chart":
                        for option in question["visual_options"]:
                            option["chart_spec"] = {**SPEC, "values": [option["number"] * 10, 100 - option["number"] * 10]}
                        c.execute("UPDATE generated_questions SET edited_json=? WHERE id=?", (json.dumps(question), qid))
    return bid


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.waits = []

    def now(self):
        return self.value

    def wait(self, seconds):
        self.waits.append(seconds)
        self.value += seconds
        return False


def settings(**kwargs):
    return MediaSettings("synthetic-project", "synthetic-bucket", "https://synthetic.cloudfront.net", **kwargs)


class FakeGoogle:
    def __init__(self):
        self.speeches = []
        self.images = []
        self.chart_prompts = []
        self.banner_prompts = []
        self.image_colors = []

    def speech(self, speaker, text):
        import wave
        self.speeches.append((speaker, text))
        data = io.BytesIO()
        with wave.open(data, "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(4800))
        return data.getvalue()

    def image(self, prompt, *, chart=False, color=False, banner=False):
        self.images.append(prompt)
        self.image_colors.append(color)
        if chart:
            self.chart_prompts.append(prompt)
        if banner:
            self.banner_prompts.append(prompt)
        data = io.BytesIO()
        # Return color even for monochrome requests to exercise output conversion.
        Image.new("RGB", (80, 60), "#35a76f").save(data, "PNG")
        return data.getvalue()

    def close(self):
        pass


def assert_image_color(data, *, color):
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.mode == "RGB"
        red, green, blue = (band.tobytes() for band in image.split())
        assert (red != green or green != blue) == color


class FakeCloud:
    bucket = "synthetic-bucket"

    def __init__(self):
        self.objects = {}
        self.uploads = 0
        self.fail = False

    def upload(self, path, key, size, sha, mime):
        self.uploads += 1
        if self.fail:
            raise MediaError("synthetic transient upload error", retryable=True)
        assert digest_file(path) == (size, sha)
        self.objects[key] = (path.read_bytes(), mime, sha)

    def verify(self, row, *, local_path=None):
        data, mime, sha = self.objects[row["storage_path"]]
        assert (len(data), mime, sha) == (row["byte_size"], row["mime_type"], row["file_sha256"])
        if local_path:
            assert digest_file(local_path) == (len(data), sha)

    def close(self):
        pass


class FakeRegistration(MediaRegistration):
    def __init__(self, root):
        super().__init__(root, "postgresql://synthetic@localhost/synthetic")
        self.registered = []

    def preflight(self):
        pass

    def register(self, package):
        self._current(package)
        self.registered.append(package)
        return "synthetic-job"


def pipeline(root, bid, *, cloud=None, google=None, registration=None, retries=0):
    store = BulkStore(root)
    registration = registration or FakeRegistration(root)
    config = {"settings": settings(tts_delay=0, image_delay=0, retries=retries).snapshot(), "target": registration.target}
    store.configure_media(bid, config)
    return MediaPipeline(root, store, bid, config, client=google or FakeGoogle(), cloud=cloud or FakeCloud(),
                         registration=registration, wait=lambda _: False)


def test_real_calls_are_paced_and_retry_after_is_honored():
    clock = Clock()
    runner = RequestRunner(settings(), now=clock.now, wait=clock.wait)
    called = []
    runner.call("tts", lambda: called.append(clock.now()))
    runner.call("tts", lambda: called.append(clock.now()))
    assert called == [1000, 1002]
    attempts = []

    def transient():
        attempts.append(clock.now())
        if len(attempts) == 1:
            raise MediaError("429", retryable=True, retry_after=60)
        return "done"
    assert runner.call("image", transient) == "done"
    assert attempts[1] - attempts[0] >= 60
    assert runner.call("image", lambda: clock.now()) >= attempts[1] + 5


def test_retry_budget_is_bounded_and_permanent_errors_are_not_retried():
    clock = Clock()
    runner = RequestRunner(settings(), now=clock.now, wait=clock.wait)
    calls = []

    def fail():
        calls.append(1)
        raise MediaError("503", retryable=True)
    with pytest.raises(MediaError):
        runner.call("image", fail)
    assert len(calls) == 6
    assert len(clock.waits) == 5
    calls.clear()

    def denied():
        calls.append(1)
        raise MediaError("403")
    with pytest.raises(MediaError):
        runner.call("upload", denied)
    assert len(calls) == 1
    stop = threading.Event()
    stop.set()
    with pytest.raises(Stopped):
        RequestRunner(settings(), stop=stop).call("tts", lambda: None)


def test_retry_after_also_delays_the_next_item_after_retry_budget_is_exhausted():
    clock = Clock()
    runner = RequestRunner(settings(retries=0), now=clock.now, wait=clock.wait)

    def limited():
        raise MediaError("429", retryable=True, retry_after=90)
    with pytest.raises(MediaError):
        runner.call("tts", limited)
    assert runner.call("tts", clock.now) >= 1090


def test_google_request_preserves_http_status_without_logging_response_or_credentials():
    class Credentials:
        valid = True
        token = "synthetic-secret"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "42"}, json={"error": "private provider response"})
    client = GoogleMediaClient(settings(), credentials=Credentials(), client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(MediaError) as error:
        client.speech("여자", "원문을 한 번만 읽습니다.")
    assert error.value.retryable and error.value.retry_after == 42
    assert "private" not in str(error.value) and "secret" not in str(error.value)
    body = json.loads(requests[0].content)
    assert body["input"]["text"] == "원문을 한 번만 읽습니다."
    assert body["audioConfig"]["audioEncoding"] == "LINEAR16"
    assert body["input"]["prompt"].startswith("Read the supplied Korean text literally, exactly once.")
    client.close()


def test_word_prompt_changes_delivery_without_changing_text_voice_or_rate():
    from topik_question_lab.common.vocabulary.vocabulary_audio import WORD_NARRATION_PROMPT

    class Credentials:
        valid = True
        token = "synthetic-token"

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"audioContent": "dGVzdA=="})

    client = GoogleMediaClient(settings(), credentials=Credentials(), client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        client.speech("여자", "가꾸다", prompt=WORD_NARRATION_PROMPT)
        client.speech("여자", "가꾸다")
    finally:
        client.close()
    assert requests[0]["input"] == {"text": "가꾸다", "prompt": WORD_NARRATION_PROMPT}
    assert requests[1]["input"]["prompt"].startswith("Read the supplied Korean text literally, exactly once.")
    assert requests[0]["voice"] == requests[1]["voice"]
    assert requests[0]["audioConfig"] == requests[1]["audioConfig"]


def test_chart_data_must_match_each_label_and_number():
    assert chart_spec(SPEC, "교통 조사 버스: 60%, 지하철: 40%")
    with pytest.raises(ValueError):
        chart_spec(SPEC, "교통 조사 버스: 40%, 지하철: 60%")
    with pytest.raises(ValueError):
        chart_spec({**SPEC, "values": [float("nan"), 40]})


@pytest.mark.parametrize("value,written", [
    (9600000, "9,600,000"), (9600000, "9600000"), (9600, "9,600"),
    (1234567.89, "1,234,567.89"), (0.123456789, "0.123456789"), (60, "60.000"),
])
def test_chart_numbers_allow_grouping_and_preserve_precision(value, written):
    spec = {**SPEC, "labels": ["버스"], "values": [value], "unit": "명"}
    assert chart_spec(spec, f"교통 조사 버스: {written}명")["values"] == [value]


@pytest.mark.parametrize("source", [
    "교통 조사 버스: 9,450,000명, 지하철: 9,600,000명",
    "교통 조사 버스: 9,600,001명, 지하철: 9,450,000명",
    "교통 조사 버스: 9,600,000% (단위 명), 지하철: 9,450,000명",
    "교통 조사 버스: 96,00,000명, 지하철: 9,450,000명",
])
def test_chart_number_normalization_does_not_hide_mismatches(source):
    spec = {**SPEC, "values": [9600000, 9450000], "unit": "명"}
    with pytest.raises(ValueError):
        chart_spec(spec, source)


def test_chart_numbers_without_units_allow_comma_separators():
    assert chart_spec({**SPEC, "unit": ""}, "교통 조사 버스: 60, 지하철: 40")


def test_invalid_speaker_is_blocked_before_media_generation(tmp_path):
    from topik_question_lab.common.media.audio_composer import narration_script
    from dataclasses import replace
    bid = seed_media(tmp_path, "listening")
    draft = FakeRegistration(tmp_path).package(bid)
    from topik_question_lab.common.question_bank.direct_publication import package_draft
    candidate = package_draft(draft).items[0].candidate
    invalid = replace(candidate, question={**candidate.question, "dialogue_turns": [{"speaker": "unknown", "text": "대본"}]})
    with pytest.raises(ValueError, match="화자"):
        narration_script([invalid])


def test_reading_pipeline_upload_failure_reuses_rendered_image_and_never_registers_partial(tmp_path):
    bid = seed_media(tmp_path)
    cloud = FakeCloud()
    cloud.fail = True
    reg = FakeRegistration(tmp_path)
    first = pipeline(tmp_path, bid, cloud=cloud, registration=reg)
    with pytest.raises(ValueError, match="필수 미디어"):
        first.run()
    assert not reg.registered
    assert any(t["kind"] == "chart" and t["status"] == "succeeded" for t in first.store.media_tasks(bid))
    cloud.fail = False
    second = pipeline(tmp_path, bid, cloud=cloud, registration=reg)
    second.store.reset_media_attempts(bid)
    second.run()
    assert len(reg.registered) == 1 and len(reg.registered[0]["media"]) == 1
    assert second.store.batches()[0]["pipeline_status"] == "succeeded"
    assert all(not c["review"]["approved"] for c in reg.registered[0]["candidates"])


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_listening_pipeline_has_35_groups_50_bindings_and_reuses_repeated_speech(tmp_path):
    bid = seed_media(tmp_path, "listening")
    google, cloud, reg = FakeGoogle(), FakeCloud(), FakeRegistration(tmp_path)
    run = pipeline(tmp_path, bid, cloud=cloud, google=google, registration=reg)
    run.run()
    assets = reg.registered[0]["media"]
    audio = [a for a in assets if a["kind"] == "audio"]
    assert len(audio) == 35
    assert sorted(p for a in audio for p in a["positions"]) == list(range(1, 51))
    assert all(a["narration_version"] == "exam_track_v4" and a["duration_ms"] > 0 for a in audio)
    assert len(google.speeches) == len(set(google.speeches))
    before = (len(google.speeches), len(google.images))
    pipeline(tmp_path, bid, cloud=cloud, google=google, registration=reg).run()
    assert (len(google.speeches), len(google.images)) == before


def test_changed_question_blocks_registration_after_upload(tmp_path):
    bid = seed_media(tmp_path)
    reg = FakeRegistration(tmp_path)
    run = pipeline(tmp_path, bid, registration=reg)
    original = run.cloud.verify

    def edit(row, **kwargs):
        original(row, **kwargs)
        change_question(tmp_path, edit=lambda q: {**q, "explanation": "변경됨"}, batch_id=bid)
    run.cloud.verify = edit
    with pytest.raises(PublicationBlocked, match="변경"):
        run.run()
    assert not reg.registered


def test_backup_contains_media_and_does_not_auto_resume_restored_jobs(tmp_path):
    from topik_question_lab.maintenance.backup_service import create_backup, restore_backup
    bid = seed_media(tmp_path)
    run = pipeline(tmp_path, bid)
    run.run()
    run.store.pipeline(bid, "tts", auto_resume=1)
    archive = create_backup(tmp_path)
    restored = tmp_path / "restored"
    restore_backup(restored, archive.data)
    assert list((restored / "data/media_generation").glob("*.png"))
    assert BulkStore(restored).batches()[0]["auto_resume"] == 0
    assert BulkStore(restored).batches()[0]["pipeline_status"] == "paused"


def test_atomic_registration_pending_review_and_promotion(postgres_database_factory, tmp_path, monkeypatch):
    import psycopg
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    monkeypatch.setenv("DATABASE_URL", url)
    bid = seed_media(tmp_path)
    reg = MediaRegistration(tmp_path, url)
    run = pipeline(tmp_path, bid, registration=reg)
    run.run()
    package = reg.jobs.list()[0]["payload"]["packages"][0]
    with psycopg.connect(url) as c:
        assert c.execute("SELECT review_status,count(*) FROM topik_bank.items GROUP BY review_status").fetchall() == [("pending_review", 50)]
        assert c.execute("SELECT review_status FROM topik_bank.question_sets").fetchall() == [("pending_review",)]
        check_media(c, package)
    run = pipeline(tmp_path, bid, cloud=run.cloud, registration=reg)
    run.run()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 50
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 1
    for path in (tmp_path / "data/types").glob("*.db"):
        with sqlite3.connect(path) as c:
            c.execute("UPDATE reviews SET data_json=?", (json.dumps({"approved": True}),))
    service = DirectPublicationService(tmp_path, url)
    job = service.prepare([bid])
    assert service.publish(job["id"])["status"] == "succeeded"
    with psycopg.connect(url) as c:
        assert c.execute("SELECT DISTINCT review_status FROM topik_bank.items").fetchall() == [("reviewed",)]
        check_media(c, package)


def test_registration_failure_rolls_back_all_rows_and_pending_edits_are_atomic(postgres_database_factory, tmp_path, monkeypatch):
    import psycopg
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    monkeypatch.setenv("DATABASE_URL", url)
    bid = seed_media(tmp_path)
    reg = MediaRegistration(tmp_path, url)
    original = reg._bind
    def fail(c, package):
        original(c, package)
        raise RuntimeError("synthetic DB failure")
    reg._bind = fail
    run = pipeline(tmp_path, bid, registration=reg)
    with pytest.raises(MediaError):
        run.run()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM topik_bank.item_visual_assets").fetchone()[0] == 0
    reg._bind = original
    run.store.reset_media_attempts(bid)
    pipeline(tmp_path, bid, cloud=run.cloud, registration=reg).run()
    change_question(tmp_path, edit=lambda q: {**q, "explanation": "수정 해설"}, batch_id=bid)
    pipeline(tmp_path, bid, cloud=run.cloud, registration=reg).run()
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 1
        assert c.execute("SELECT explanation FROM topik_bank.items WHERE position=1").fetchone()[0] == "수정 해설"


def test_auto_runner_retries_transient_text_and_starts_media_only_after_50_items(tmp_path, monkeypatch):
    from test_bulk_generation import prepared, caller_for, wait
    import topik_question_lab.common.media.media_generation as generation
    import topik_question_lab.common.media.media_provider as provider
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    calls = []
    normal = caller_for(snapshots, calls)
    first = True

    def transient(*args, **kwargs):
        nonlocal first
        result = normal(*args, **kwargs)
        if first:
            first = False
            return result.model_copy(update={"parsed_json": None, "error": "synthetic timeout", "error_retryable": True})
        return result
    config = {"settings": settings(tts_delay=0, image_delay=0).snapshot(), "target": "synthetic-target"}
    monkeypatch.setattr(generation, "preflight", lambda *args: config)
    clock = Clock()
    real_requests = RequestRunner
    monkeypatch.setattr(provider, "RequestRunner", lambda *args, **kwargs: real_requests(*args, **kwargs, wait=clock.wait, now=clock.now))
    media_started = []

    class Pipeline:
        def __init__(self, root, store, bid, config, **kwargs):
            self.store, self.bid = store, bid

        def run(self):
            assert sum(t["count"] for t in self.store.tasks(self.bid)) == 50
            media_started.append(self.bid)
            self.store.pipeline(self.bid, "succeeded", auto_resume=0)
    monkeypatch.setattr(generation, "MediaPipeline", Pipeline)
    runner = BulkRunner(tmp_path, transient)
    bid = runner.start(snapshots, media_config=config)
    wait(runner)
    assert len(calls) == 19 and media_started == [bid]
    assert runner.store.batches()[0]["pipeline_status"] == "succeeded"
    assert clock.waits and min(clock.waits) >= 10


def test_interrupted_auto_job_resumes_without_a_second_click(tmp_path, monkeypatch):
    store = BulkStore(tmp_path)
    with store.connect() as c:
        c.execute("INSERT INTO batches(id,section,provider,model,status) VALUES ('resume','reading','test','test','running')")
    store.configure_media("resume", {"settings": settings().snapshot(), "target": "synthetic"})
    executed = []

    def finish(self, bid, lock):
        try:
            executed.append(bid)
            self.store.set_status(bid, "succeeded")
            self.store.pipeline(bid, "succeeded", auto_resume=0)
        finally:
            lock.__exit__(None, None, None)
    monkeypatch.setattr(BulkRunner, "_execute", finish)
    runner = BulkRunner(tmp_path)
    runner.thread.join(5)
    assert executed == ["resume"]
    runner.recover_idle()
    assert executed == ["resume"]


def test_missing_media_config_blocks_ui_before_any_model_call(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    import topik_question_lab.common.generation.bulk_generation as bulk
    from test_bulk_generation import prepared
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    monkeypatch.setattr(bulk, "prepare_batch", lambda root, section, *args: (snapshots, []) if section == "reading" else ([], ["not ready"]))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "topik_question_lab/menus/bulk_generation/bulk_generation_app.py"), default_timeout=30).run()
    assert not any(c.label == "자동 검수·수정" for c in app.checkbox)
    assert next(c for c in app.checkbox if c.label == "음원·이미지 생성 및 DB 자동 등록").value
    assert not next(c for c in app.checkbox if c.label == "음원·이미지 생성 및 DB 자동 등록").disabled
    app.radio(key="bulk-section").set_value("reading").run()
    app.button(key="bulk-start-reading").click().run()
    assert not app.exception and any("S3_BUCKET" in e.value for e in app.error)
    assert BulkStore(tmp_path).batches() == []


def test_recovery_preflight_failure_releases_worker_and_keeps_model_uncalled(tmp_path, monkeypatch):
    from test_bulk_generation import prepared, caller_for, wait
    import topik_question_lab.common.media.media_generation as generation
    snapshots = prepared(tmp_path, "reading", monkeypatch)
    calls = []
    config = {"settings": settings().snapshot(), "target": "synthetic-target"}

    def unavailable(*args):
        raise ValueError("설정이 변경되었습니다.")
    monkeypatch.setattr(generation, "preflight", unavailable)
    runner = BulkRunner(tmp_path, caller_for(snapshots, calls))
    runner.start(snapshots, media_config=config)
    wait(runner)
    batch = runner.store.batches()[0]
    assert batch["status"] == "interrupted" and batch["pipeline_status"] == "failed" and not batch["auto_resume"]
    assert not calls
    from topik_question_lab.common.generation.bulk_generation import execution_lock
    with execution_lock(tmp_path):
        pass


def test_listening_audio_rows_and_all_50_bindings_are_atomic(postgres_database_factory, tmp_path, monkeypatch):
    import psycopg
    import topik_question_lab.common.media.media_generation as generation
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    monkeypatch.setenv("DATABASE_URL", url)
    bid = seed_media(tmp_path, "listening")

    def synthetic_compose(script, speech, path, config):
        path.write_bytes(b"synthetic-mp3-" + json.dumps(script, ensure_ascii=False).encode())
        return 1000
    monkeypatch.setattr(generation, "compose", synthetic_compose)
    monkeypatch.setattr(generation, "validate_mp3", lambda *args: 1000)
    reg = MediaRegistration(tmp_path, url)
    run = pipeline(tmp_path, bid, registration=reg)
    run.run()
    package = reg.jobs.list()[0]["payload"]["packages"][0]
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.tts_audio_assets").fetchone()[0] == 35
        assert c.execute("SELECT count(*) FROM topik_bank.item_audio_bindings WHERE is_current").fetchone()[0] == 50
        check_media(c, package)
    incomplete = {**package, "media": [a for a in package["media"] if a.get("positions") != [49, 50]]}
    with pytest.raises(PublicationBlocked, match="전체|필수"):
        reg.register(incomplete)
    with psycopg.connect(url) as c:
        check_media(c, package)


def test_commit_response_loss_reconciles_without_duplicate_rows(postgres_database_factory, tmp_path, monkeypatch):
    import psycopg
    from contextlib import contextmanager
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    monkeypatch.setenv("DATABASE_URL", url)
    bid = seed_media(tmp_path)
    reg = MediaRegistration(tmp_path, url)
    connect = reg.connect
    lost = False

    @contextmanager
    def response_lost(readonly=False):
        nonlocal lost
        with connect(readonly=readonly) as c:
            yield c
        if not readonly and not lost:
            lost = True
            raise psycopg.OperationalError("synthetic commit acknowledgement lost")
    reg.connect = response_lost
    run = pipeline(tmp_path, bid, registration=reg)
    run.run()
    assert lost and reg.jobs.list()[0]["status"] == "succeeded"
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM topik_bank.items").fetchone()[0] == 50
        assert c.execute("SELECT count(*) FROM topik_bank.question_sets").fetchone()[0] == 1
