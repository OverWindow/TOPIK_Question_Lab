import shutil
import subprocess
import wave
from types import SimpleNamespace

import pytest

from topik_question_lab.common.media.audio_composer import compose, validate_mp3


@pytest.mark.parametrize("failure", [None, "encode", "duration", "decode", "timeout"])
def test_intermediate_wav_is_removed_only_after_success(tmp_path, monkeypatch, failure):
    import topik_question_lab.common.media.audio_composer as composer

    target = tmp_path / "track.part.mp3"
    intermediate = target.with_suffix(".wav")
    existing = tmp_path / "previous.part.wav"
    existing.write_bytes(b"keep existing file")
    settings = SimpleNamespace(ffmpeg="synthetic-ffmpeg", ffprobe="synthetic-ffprobe")

    def run(args, **kwargs):
        assert intermediate.exists()
        if "-codec:a" in args:
            with wave.open(str(intermediate), "rb") as source:
                assert source.getnframes() == 2400
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 120)
            target.write_bytes(b"synthetic mp3")
            return SimpleNamespace(returncode=int(failure == "encode"))
        if args[0] == settings.ffprobe:
            return SimpleNamespace(returncode=0, stdout=b"0" if failure == "duration" else b"0.1")
        assert "null" in args
        return SimpleNamespace(returncode=int(failure == "decode"))

    monkeypatch.setattr(composer.subprocess, "run", run)
    script = {"segments": [{"kind": "silence", "durationMs": 100}]}
    if failure:
        with pytest.raises(subprocess.TimeoutExpired if failure == "timeout" else ValueError):
            compose(script, None, target, settings)
        assert intermediate.exists()
    else:
        assert compose(script, None, target, settings) == 100
        assert target.read_bytes() == b"synthetic mp3"
        assert not intermediate.exists()
    assert existing.read_bytes() == b"keep existing file"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_real_mp3_remains_decodable_after_intermediate_cleanup(tmp_path):
    target = tmp_path / "track.part.mp3"
    settings = SimpleNamespace(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
    duration = compose({"segments": [{"kind": "silence", "durationMs": 100}]}, None, target, settings)
    assert duration > 0 and validate_mp3(target, settings) == duration
    assert not target.with_suffix(".wav").exists()
