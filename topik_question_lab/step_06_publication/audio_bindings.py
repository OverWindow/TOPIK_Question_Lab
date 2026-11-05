"""Current item audio links with append-only replacement history."""
from __future__ import annotations


def require_item_audio_schema(c):
    ready = c.execute("""SELECT
        to_regclass('topik_bank.question_set_item_audio_bindings') IS NULL
        AND EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema='topik_bank' AND table_name='item_audio_bindings' AND column_name='binding_id')""").fetchone()[0]
    if not ready:
        raise ValueError("문항 음원 통합 마이그레이션(011_item_audio_only)이 필요합니다. 백업 후 수동 적용하세요.")


def bind_audio(c, item_id, content_hash, audio_asset_id, source_hash):
    # Lock the owner even when it has no existing binding.
    row = c.execute("SELECT content_hash FROM topik_bank.items WHERE item_id=%s FOR UPDATE", (item_id,)).fetchone()
    if not row or row[0] != content_hash:
        raise ValueError("음원을 연결할 문항의 내용이 변경되었습니다.")
    current = c.execute("""SELECT binding_id,content_hash,audio_asset_id,source_hash
        FROM topik_bank.item_audio_bindings WHERE item_id=%s AND is_current""", (item_id,)).fetchone()
    if current and (current[1], str(current[2]), current[3]) == (content_hash, str(audio_asset_id), source_hash):
        return current[0]
    c.execute("UPDATE topik_bank.item_audio_bindings SET is_current=false WHERE item_id=%s AND is_current", (item_id,))
    return c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
        VALUES (%s,%s,%s,%s) RETURNING binding_id""", (item_id,content_hash,audio_asset_id,source_hash)).fetchone()[0]
