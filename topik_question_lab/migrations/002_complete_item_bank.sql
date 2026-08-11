ALTER TABLE topik_bank.item_versions
    ADD COLUMN IF NOT EXISTS type_slot SMALLINT;

UPDATE topik_bank.item_versions
SET type_slot = (content_json ->> 'type_slot')::SMALLINT
WHERE type_slot IS NULL;

ALTER TABLE topik_bank.item_versions
    ALTER COLUMN type_slot SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'item_versions_type_slot_check'
          AND conrelid = 'topik_bank.item_versions'::regclass
    ) THEN
        ALTER TABLE topik_bank.item_versions
            ADD CONSTRAINT item_versions_type_slot_check CHECK (type_slot BETWEEN 1 AND 50);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS item_versions_slot_idx
    ON topik_bank.item_versions(section, generator_version, type_slot);

CREATE OR REPLACE VIEW topik_bank.current_items AS
SELECT DISTINCT ON (i.item_id)
    i.source_key,
    i.created_at AS item_created_at,
    v.*
FROM topik_bank.items i
JOIN topik_bank.item_versions v ON v.item_id = i.item_id
ORDER BY i.item_id, v.item_version DESC;

CREATE OR REPLACE VIEW topik_bank.current_set_contents AS
WITH latest_sets AS (
    SELECT DISTINCT ON (v.set_id)
        v.set_id,
        v.set_version,
        v.review_status AS set_review_status,
        v.default_target_level,
        v.default_predicted_difficulty,
        v.published_at
    FROM topik_bank.question_set_versions v
    ORDER BY v.set_id, v.set_version DESC
)
SELECT
    s.set_id,
    ls.set_version,
    s.section AS set_section,
    s.generator_provider AS set_generator_provider,
    s.generator_model AS set_generator_model,
    s.generator_version AS set_generator_version,
    ls.set_review_status,
    ls.default_target_level,
    ls.default_predicted_difficulty,
    ls.published_at,
    si.position,
    i.source_key,
    iv.item_id,
    iv.item_version,
    iv.type_slot,
    iv.item_type,
    iv.primary_skill,
    iv.target_level,
    iv.predicted_difficulty,
    iv.irt_difficulty,
    iv.irt_discrimination,
    iv.review_status AS item_review_status,
    iv.stem,
    iv.choices,
    iv.correct_answer,
    iv.explanation,
    iv.content_json,
    iv.source_provenance
FROM topik_bank.question_sets s
JOIN latest_sets ls ON ls.set_id = s.set_id
JOIN topik_bank.question_set_items si
  ON si.set_id = ls.set_id AND si.set_version = ls.set_version
JOIN topik_bank.items i ON i.item_id = si.item_id
JOIN topik_bank.item_versions iv
  ON iv.item_id = si.item_id AND iv.item_version = si.item_version;

