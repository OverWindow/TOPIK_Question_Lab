from __future__ import annotations

from typing import TypeVar

from topik_question_lab.common.paths import PACKAGE_ROOT


MENU_PAGES = {
    "app.py": "reading/app.py",
    "listening_app.py": "listening/listening_app.py",
    "bulk_generation_app.py": "bulk_generation/bulk_generation_app.py",
    "storage_overview_app.py": "storage_overview/storage_overview_app.py",
    "bank_editor_app.py": "bank_editor/bank_editor_app.py",
    "topic_bank_app.py": "topic_bank/topic_bank_app.py",
    "postgres_publish_app.py": "registration/postgres_publish_app.py",
    "bank_transfer_app.py": "bank_transfer/bank_transfer_app.py",
    "backup_restore_app.py": "backup_restore/backup_restore_app.py",
    "vocabulary_app.py": "vocabulary/vocabulary_app.py",
    "vocabulary_questions_app.py": "vocabulary_generation/vocabulary_questions_app.py",
    "vocabulary_question_editor_app.py": "vocabulary_editor/vocabulary_question_editor_app.py",
    "vocabulary_transfer_app.py": "vocabulary_transfer/vocabulary_transfer_app.py",
    "korean_dictionary_app.py": "korean_dictionary/korean_dictionary_app.py",
    "postgres_cleanup_app.py": "postgres_cleanup/postgres_cleanup_app.py",
}


def menu_page(filename: str):
    return PACKAGE_ROOT / "menus" / MENU_PAGES[filename]


Item = TypeVar("Item")


def next_sequence_item(items: list[Item], current: Item) -> Item | None:
    """Return the next displayed item without wrapping at the end."""
    try:
        index = items.index(current)
    except ValueError:
        return None
    return items[index + 1] if index + 1 < len(items) else None
