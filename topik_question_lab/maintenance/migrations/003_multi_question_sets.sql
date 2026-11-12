ALTER TABLE topik_bank.question_sets
    ADD COLUMN IF NOT EXISTS set_sequence INTEGER;

-- Before this migration the identity columns were unique, so every existing
-- model/section identity has exactly one logical set. Preserve those set IDs
-- and assign them sequence 1.
UPDATE topik_bank.question_sets
SET set_sequence = 1
WHERE set_sequence IS NULL;

ALTER TABLE topik_bank.question_sets
    ALTER COLUMN set_sequence SET NOT NULL;

ALTER TABLE topik_bank.question_sets
    DROP CONSTRAINT IF EXISTS question_sets_section_generator_provider_generator_model_ge_key;

-- Also handle databases where the original four-column UNIQUE constraint was
-- created under a non-default name.
DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'topik_bank.question_sets'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) =
              'UNIQUE (section, generator_provider, generator_model, generator_version)'
    LOOP
        EXECUTE format(
            'ALTER TABLE topik_bank.question_sets DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'question_sets_set_sequence_check'
          AND conrelid = 'topik_bank.question_sets'::regclass
    ) THEN
        ALTER TABLE topik_bank.question_sets
            ADD CONSTRAINT question_sets_set_sequence_check CHECK (set_sequence >= 1);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'question_sets_identity_sequence_key'
          AND conrelid = 'topik_bank.question_sets'::regclass
    ) THEN
        ALTER TABLE topik_bank.question_sets
            ADD CONSTRAINT question_sets_identity_sequence_key UNIQUE (
                section,
                generator_provider,
                generator_model,
                generator_version,
                set_sequence
            );
    END IF;
END $$;

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
    iv.source_provenance,
    s.set_sequence
FROM topik_bank.question_sets s
JOIN latest_sets ls ON ls.set_id = s.set_id
JOIN topik_bank.question_set_items si
  ON si.set_id = ls.set_id AND si.set_version = ls.set_version
JOIN topik_bank.items i ON i.item_id = si.item_id
JOIN topik_bank.item_versions iv
  ON iv.item_id = si.item_id AND iv.item_version = si.item_version;
