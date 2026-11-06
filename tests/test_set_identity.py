from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from streamlit.testing.v1 import AppTest

from topik_question_lab.step_06_publication.direct_publication import batch_set_id
from topik_question_lab.ui.set_identity import korean_datetime, korean_time, set_view, set_title, set_label, set_status


def records():
    batch = {"id": "generation-one", "section": "reading", "exam_format": "pbt", "model": "model-one",
             "created_at": "2026-11-05 15:30:00", "local_count": 50, "local_approved_count": 50}
    stored = {"set_id": UUID(batch_set_id("reading", batch["id"])), "section": "reading", "exam_format": "pbt",
              "generator_version": "model-one", "set_sequence": 3, "item_count": 50, "review_status": "reviewed",
              "published_at": "2026-11-06T01:00:00Z", "coverage": {
                  "audio_bound": 0, "audio_required": 0, "images_ready": 1, "images_required": 1}}
    return batch, stored


def test_identity_survives_registration_and_local_question_cleanup():
    batch, stored = records()
    before = deepcopy((batch, stored))
    generated = set_view(batch)
    registered = set_view(batch, sets=[stored])
    bank = set_view(stored, batches=[batch])
    assert generated["code"] == registered["code"] == bank["code"]
    assert set_title(generated) == set_title(bank) == "PBT 읽기 · 2026/11/06 00:30 생성"
    assert set_label(registered) == set_label(bank)
    assert "등록 3번" in set_label(bank)
    assert "미등록" in set_status(generated)
    assert "미디어 연결 완료 · 등록 완료" in set_status(registered)
    assert (batch, stored) == before
    cleaned = set_view({**batch, "local_count": 0, "local_approved_count": 0}, sets=[stored])
    assert "문항 50/50 · 검수 승인" in set_status(cleaned)


def test_legacy_set_uses_registration_date_without_inventing_generation_time():
    _, stored = records()
    legacy = set_view(stored)
    assert set_title(legacy) == "PBT 읽기 · 등록 세트 3번"
    assert legacy["generation_created_at"] is None
    app = AppTest.from_string('''
from topik_question_lab.ui.set_identity import render_set_summary
import streamlit as st
render_set_summary(st.session_state["view"])
''')
    app.session_state["view"] = legacy
    app.run()
    assert not app.exception
    assert any("생성 시각 미기록 · 등록일 2026/11/06 10:00" in c.value for c in app.caption)


def test_simultaneous_generations_and_exam_formats_have_distinct_labels():
    batch, _ = records()
    views = [set_view(batch), set_view({**batch, "id": "generation-two"}),
             set_view({**batch, "id": "generation-ibt", "exam_format": "ibt", "local_count": 30})]
    assert len({set_label(v) for v in views}) == 3
    assert len({v["code"] for v in views}) == 3
    assert set_title(views[2]).startswith("IBT 읽기")
    assert "문항 30/30" in set_status(views[2])


@pytest.mark.parametrize("value", ["2026-11-05 15:30:00", "2026-11-05T15:30:00Z",
    "2026-11-06T00:30:00+09:00", datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc)])
def test_korean_dates_match_across_sqlite_and_postgres_timestamps(value):
    assert korean_datetime(value).date() == date(2026, 11, 6)
    assert korean_time(value) == "2026/11/06 00:30"


def test_unavailable_bank_and_partial_sets_are_not_reported_as_ready():
    batch, stored = records()
    unavailable = set_view(batch, bank_available=False)
    assert "등록 확인 불가" in set_status(unavailable)
    assert "등록 완료" not in set_status(unavailable)
    partial = set_view({**stored, "item_count": 49})
    assert "미디어 연결 미완료" in set_status(partial)
    assert "구성 미완성" in set_status(partial)
    assert "미디어 확인 불가" in set_status(set_view({**stored, "coverage": None}))
    assert korean_datetime(None) is None and korean_time("invalid") == "기록 없음"


def test_registration_and_editor_show_the_same_selected_set(monkeypatch):
    from topik_question_lab.step_06_publication import bank_editor, direct_publication, storage_overview
    batch, stored = records()
    stored.update(set_id=str(stored["set_id"]), item_count=1, coverage=None)
    item = {"item_id": "00000000-0000-0000-0000-000000000001", "set_id": stored["set_id"],
            "section": "reading", "type_slot": 1, "position": 1, "item_type": "grammar_blank",
            "stem": "문장 ( )", "review_status": "reviewed", "target_level": 4, "primary_skill": "문법",
            "source_provenance": {}, "content_hash": "synthetic", "content_json": {
                "question_type": "grammar_blank", "type_slot": 1, "stem": "문장 ( )",
                "choices": ["가", "나", "다", "라"], "answer": 1, "explanation": "해설"}}
    bank = {"available": True, "media_available": False, "sets": [stored], "items": [item]}
    before = deepcopy(bank)
    registered = AppTest.from_string('''
import streamlit as st
from topik_question_lab.ui.registration_ui import render_sets
render_sets(st.session_state["snapshot"], "synthetic")
''')
    registered.session_state["snapshot"] = {"bank": bank, "batches": [batch], "checked_at": "2026-11-06T00:00:00Z"}
    registered.run()
    assert not registered.exception and not registered.error
    expected = set_label(set_view(stored, batches=[batch]))
    assert next(s for s in registered.selectbox if s.label == "확인할 세트").options == [expected]

    snapshot = {"target": "synthetic", "item": item, "items": [item], "set": stored,
                "media": {"item_audio_bindings": [], "tts_audio_assets": [], "item_visual_assets": []}}
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic@localhost/synthetic")
    monkeypatch.setattr(storage_overview, "read_bank", lambda _: deepcopy(bank))
    monkeypatch.setattr(direct_publication, "read_batches", lambda _: [deepcopy(batch)])
    monkeypatch.setattr(bank_editor, "BankEditor", lambda _: SimpleNamespace(target="synthetic", load=lambda _: deepcopy(snapshot)))
    page = Path(__file__).resolve().parents[1] / "topik_question_lab/ui/bank_editor_app.py"
    editor = AppTest.from_file(str(page), default_timeout=20).run()
    assert not editor.exception and not editor.error
    assert expected in next(s for s in editor.selectbox if s.label == "세트 필터").options
    assert any(s.value == set_title(set_view(stored, batches=[batch])) for s in editor.subheader)
    assert bank == before
