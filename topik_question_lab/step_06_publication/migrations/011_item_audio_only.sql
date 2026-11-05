-- Preserve both legacy histories before retiring set-addressed audio links.
LOCK TABLE topik_bank.items, topik_bank.tts_audio_assets,
    topik_bank.item_audio_bindings, topik_bank.question_set_item_audio_bindings
    IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMP TABLE legacy_item_audio ON COMMIT DROP AS
    SELECT * FROM topik_bank.item_audio_bindings;
CREATE TEMP TABLE legacy_set_audio ON COMMIT DROP AS
    SELECT b.*, i.item_id, i.content_hash
    FROM topik_bank.question_set_item_audio_bindings b
    LEFT JOIN topik_bank.items i USING (set_id, position);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM legacy_set_audio WHERE item_id IS NULL) THEN
        RAISE EXCEPTION 'An audio binding has no matching item; migration cancelled.';
    END IF;
END $$;

ALTER TABLE topik_bank.item_audio_bindings
    ADD COLUMN binding_id UUID,
    ADD COLUMN origin_set_id UUID,
    ADD COLUMN origin_position SMALLINT,
    DROP CONSTRAINT item_audio_bindings_pkey;
ALTER TABLE topik_bank.item_audio_bindings ALTER COLUMN content_hash DROP NOT NULL;
DROP INDEX topik_bank.item_audio_current_hash_idx;

-- Deterministic IDs keep identical legacy records identical across local banks.
UPDATE topik_bank.item_audio_bindings SET
    binding_id=md5('item_audio:' || item_id::text || ':' || content_hash || ':' || audio_asset_id::text)::uuid,
    is_current=false;
ALTER TABLE topik_bank.item_audio_bindings
    ALTER COLUMN binding_id SET DEFAULT gen_random_uuid(),
    ALTER COLUMN binding_id SET NOT NULL,
    ADD PRIMARY KEY (binding_id);

INSERT INTO topik_bank.item_audio_bindings
    (binding_id,item_id,content_hash,audio_asset_id,source_hash,is_current,created_at,origin_set_id,origin_position)
SELECT md5('set_audio:' || b.set_id::text || ':' || b.position::text || ':' || b.audio_asset_id::text)::uuid,
    b.item_id, CASE WHEN b.is_current AND a.deleted_at IS NULL THEN b.content_hash END,
    b.audio_asset_id,b.source_hash,b.is_current AND a.deleted_at IS NULL,b.created_at,b.set_id,b.position
FROM legacy_set_audio b JOIN topik_bank.tts_audio_assets a USING (audio_asset_id);

UPDATE topik_bank.item_audio_bindings b SET is_current=true
FROM legacy_item_audio old, topik_bank.items i, topik_bank.tts_audio_assets a
WHERE b.origin_set_id IS NULL AND b.item_id=old.item_id
    AND b.content_hash=old.content_hash AND b.audio_asset_id=old.audio_asset_id
    AND i.item_id=b.item_id AND i.content_hash=b.content_hash
    AND a.audio_asset_id=b.audio_asset_id AND a.deleted_at IS NULL AND old.is_current
    AND NOT EXISTS (SELECT 1 FROM topik_bank.item_audio_bindings current
                    WHERE current.item_id=b.item_id AND current.is_current);

ALTER TABLE topik_bank.item_audio_bindings
    ADD CONSTRAINT item_audio_current_hash_check CHECK (NOT is_current OR content_hash IS NOT NULL),
    ADD CONSTRAINT item_audio_origin_check CHECK ((origin_set_id IS NULL) = (origin_position IS NULL)),
    ADD CONSTRAINT item_audio_origin_position_check CHECK (origin_position > 0);
CREATE UNIQUE INDEX item_audio_current_idx ON topik_bank.item_audio_bindings(item_id) WHERE is_current;
CREATE INDEX item_audio_item_idx ON topik_bank.item_audio_bindings(item_id);
CREATE INDEX item_audio_asset_idx ON topik_bank.item_audio_bindings(audio_asset_id,is_current);

DO $$ BEGIN
    IF (SELECT count(*) FROM topik_bank.item_audio_bindings) <>
       (SELECT count(*) FROM legacy_item_audio) + (SELECT count(*) FROM legacy_set_audio)
    OR EXISTS (
        SELECT item_id,content_hash,audio_asset_id,source_hash,created_at FROM legacy_item_audio
        EXCEPT ALL
        SELECT item_id,content_hash,audio_asset_id,source_hash,created_at
        FROM topik_bank.item_audio_bindings WHERE origin_set_id IS NULL
    ) OR EXISTS (
        SELECT item_id,audio_asset_id,source_hash,created_at,set_id,position FROM legacy_set_audio
        EXCEPT ALL
        SELECT item_id,audio_asset_id,source_hash,created_at,origin_set_id,origin_position
        FROM topik_bank.item_audio_bindings WHERE origin_set_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Audio binding history verification failed; migration cancelled.';
    END IF;
END $$;

-- RESTRICT intentionally blocks removal when another object still depends on it.
DROP TABLE topik_bank.question_set_item_audio_bindings;
