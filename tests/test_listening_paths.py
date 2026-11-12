import json
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from topik_question_lab.menus.listening.listening_importer import load_import_manifest, load_imported_examples, merge_page_transcription, placeholder_examples, save_imported_examples
from topik_question_lab.common.listening_storage import ListeningStorage


ROOT = Path(__file__).resolve().parents[1]


def moved_import(root, old_root="C:/former/Unigate"):
    imports = root / "data/listening/imports"
    exam_dir = imports / "sample"
    page = exam_dir / "pages/page-01.png"
    page.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "white").save(page)
    sources = root / "TOPIK-II-Listening-Script"
    sources.mkdir()
    (sources / "sample.pdf").write_bytes(b"synthetic PDF placeholder")
    manifest = {
        "exam": "sample", "page_count": 26, "sha256": "synthetic",
        "source_pdf": old_root + "/TOPIK-II-Listening-Script/sample.pdf",
        "pages": [old_root + f"/data/listening/imports/sample/pages/page-{n:02d}.png" for n in range(1, 27)],
    }
    manifest_path = exam_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    examples = placeholder_examples(manifest)
    save_imported_examples(exam_dir, examples)
    return imports, sources, manifest_path, page, examples


@pytest.mark.parametrize("old_root", ["C:\\former\\Unigate", "/former/Unigate"])
def test_moved_manifest_and_questions_resolve_without_rewriting_records(tmp_path, old_root):
    imports, sources, manifest_path, page, _ = moved_import(tmp_path, old_root)
    questions = manifest_path.parent / "questions.json"
    original = manifest_path.read_bytes(), questions.read_bytes()
    manifest = load_import_manifest(manifest_path, sources)
    assert Path(manifest['pages'][0]) == page
    assert Path(manifest['source_pdf']) == sources / 'sample.pdf'
    assert Path(load_imported_examples(imports)[0].source_page_image) == page
    assert (manifest_path.read_bytes(), questions.read_bytes()) == original


def test_transcription_after_move_updates_current_import_directory(tmp_path):
    imports, sources, manifest_path, _, _ = moved_import(tmp_path)
    manifest = load_import_manifest(manifest_path, sources)
    merge_page_transcription(manifest, {'questions': [{
        'question_number': 1, 'dialogue_turns': [{'speaker': '여자', 'text': '합성 대본'}],
        'question_prompt': '들은 내용은?',
    }]}, 'synthetic-model')
    example = load_imported_examples(imports)[0]
    assert example.dialogue_turns[0].text == '합성 대본'
    assert example.transcription_model == 'synthetic-model'


@pytest.mark.parametrize('missing_image', [False, True])
def test_listening_page_handles_moved_and_missing_images(tmp_path, missing_image):
    imports, _, _, page, examples = moved_import(tmp_path)
    db = tmp_path / 'data/listening/types' / f'{examples[0].question_type}.db'
    db.parent.mkdir(parents=True)
    storage = ListeningStorage(db)
    storage.upsert_examples([examples[0]])
    if missing_image:
        page.unlink()
    app = AppTest.from_file(str(ROOT / 'topik_question_lab/menus/listening/listening_app.py'), default_timeout=20).run()
    assert not app.exception
    if missing_image:
        assert any('페이지 이미지가 없습니다' in item.value for item in app.warning)
        assert next(b for b in app.button if b.label == '이 페이지 자동 전사').disabled
    else:
        # Existing SQLite rows retain their stored values; previews resolve paths in memory.
        assert storage.list_examples()[0].source_page_image == examples[0].source_page_image
        assert Path(load_imported_examples(imports)[0].source_page_image) == page
