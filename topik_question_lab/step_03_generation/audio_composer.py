"""Deterministic exam_track_v4 narration and local PCM/MP3 composition."""
from __future__ import annotations

import io
import math
import re
import struct
import subprocess
import wave

VERSION = "exam_track_v4"
COMPOSER_VERSION = "hub_pcm_v1"


def narration_script(items):
    ordered = sorted(items, key=lambda c: c.slot)
    turns = ordered[0].question["dialogue_turns"]
    if not turns or any(t.get("speaker") not in ("남자", "여자") or not utterances(t.get("text", "")) for t in turns):
        raise ValueError("듣기 대본에는 남자·여자 화자와 낭독할 문장이 필요합니다.")
    if any(c.question["dialogue_turns"] != turns for c in ordered):
        raise ValueError("공통 음원 문항의 대본이 다릅니다.")
    segments = [{"kind": "bell"}]
    if len(ordered) == 1:
        c = ordered[0]
        segments += [{"kind": "speech", "role": "instruction", "speaker": "여자",
                      "text": f"{c.slot}번. {c.question['question_prompt'].strip()}"},
                     {"kind": "dialogue", "repeatIndex": 1, "turns": turns}]
    else:
        segments += [{"kind": "speech", "role": "instruction", "speaker": "여자", "text": "다음을 듣고 물음에 답하십시오."},
                     {"kind": "dialogue", "repeatIndex": 1, "turns": turns},
                     {"kind": "speech", "role": "reread", "speaker": "여자", "text": "다시 읽겠습니다."},
                     {"kind": "dialogue", "repeatIndex": 2, "turns": turns}]
        segments += [{"kind": "speech", "role": "question_number", "speaker": "여자", "text": f"{c.slot}번."} for c in ordered]
    with_gaps = []
    for index, segment in enumerate(segments):
        if index:
            with_gaps.append({"kind": "silence", "durationMs": 1000})
        with_gaps.append(segment)
    return {"version": VERSION, "kind": "single" if len(ordered) == 1 else "common",
            "positions": [c.slot for c in ordered], "segments": with_gaps}


def utterances(text):
    return [p.strip() for p in re.findall(r"[^.!?。！？]+[.!?。！？]*", text.strip()) if p.strip()]


def pcm(data):
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 24000, "NONE"):
                raise ValueError("24kHz 16비트 모노 WAV가 필요합니다.")
            result = audio.readframes(audio.getnframes())
            if not result or len(result) != audio.getnframes() * 2:
                raise ValueError("음성 WAV가 비어 있거나 손상되었습니다.")
            return result
    except (wave.Error, EOFError):
        raise ValueError("음성 WAV 파일이 올바르지 않습니다.") from None


def bell_pcm():
    output = bytearray()
    for frame in range(round(24000 * .620)):
        sample = 0
        for frequency, start, length in ((783.99, 0, .320), (1046.5, .150, .470)):
            t = frame / 24000 - start
            if 0 <= t < length:
                envelope = min(1, t / .006, (length - t) / .070) * math.exp(-3.5 * t / length)
                sample += sum(gain * math.sin(2 * math.pi * frequency * harmonic * t)
                              for harmonic, gain in ((1, .72), (2.01, .2), (3.98, .08))) * envelope
        output.extend(struct.pack("<h", max(-32768, min(32767, round(sample * .24 * 32767)))))
    return bytes(output)


def compose(script, speech, target, settings):
    parts = []
    for segment in script["segments"]:
        kind = segment["kind"]
        if kind == "bell":
            parts.append(bell_pcm())
        elif kind == "silence":
            parts.append(bytes(48 * segment["durationMs"]))
        else:
            turns = segment["turns"] if kind == "dialogue" else [segment]
            for turn in turns:
                parts.extend(pcm(speech(turn["speaker"], text)) for text in utterances(turn["text"]))
    wav_path = target.with_suffix(".wav")
    with wave.open(str(wav_path), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        for part in parts:
            output.writeframesraw(part)
    result = subprocess.run([settings.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(wav_path),
                             "-codec:a", "libmp3lame", "-b:a", "64k", str(target)], capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError("음원 MP3 조립에 실패했습니다.")
    return validate_mp3(target, settings)


def validate_mp3(path, settings):
    result = subprocess.run([settings.ffprobe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, timeout=30)
    try:
        duration = float(result.stdout)
        if result.returncode or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        decoded = subprocess.run([settings.ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
                                  "-f", "null", "-"], capture_output=True, timeout=120)
        if decoded.returncode:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("생성 음원을 해독하거나 재생 길이를 확인할 수 없습니다.") from None
    return round(duration * 1000)
