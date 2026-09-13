-- Align the authoring question bank with Unigate-Web's item-only version model.
-- Only the latest legacy set version remains as the current membership pointer.

LOCK TABLE topik_bank.question_set_versions,
           topik_bank.question_set_items
    IN ACCESS EXCLUSIVE MODE;

CREATE TEMP TABLE latest_question_set_versions ON COMMIT DROP AS
SELECT DISTINCT ON (set_id)
       set_id,
       set_version,
       review_status,
       default_target_level,
       default_predicted_difficulty,
       set_fingerprint,
       published_at
  FROM topik_bank.question_set_versions
 ORDER BY set_id, set_version DESC;

CREATE UNIQUE INDEX latest_question_set_versions_set_id_idx
    ON latest_question_set_versions(set_id);

ALTER TABLE topik_bank.question_sets
    ADD COLUMN review_status TEXT,
    ADD COLUMN default_target_level SMALLINT,
    ADD COLUMN default_predicted_difficulty DOUBLE PRECISION,
    ADD COLUMN set_fingerprint CHAR(64),
    ADD COLUMN published_at TIMESTAMPTZ;

UPDATE topik_bank.question_sets question_set
   SET review_status=latest.review_status,
       default_target_level=latest.default_target_level,
       default_predicted_difficulty=latest.default_predicted_difficulty,
       set_fingerprint=latest.set_fingerprint,
       published_at=latest.published_at
  FROM latest_question_set_versions latest
 WHERE latest.set_id=question_set.set_id;

ALTER TABLE topik_bank.question_sets
    ALTER COLUMN review_status SET DEFAULT 'reviewed',
    ALTER COLUMN review_status SET NOT NULL,
    ALTER COLUMN default_target_level SET NOT NULL,
    ALTER COLUMN default_predicted_difficulty SET NOT NULL,
    ALTER COLUMN set_fingerprint SET NOT NULL,
    ALTER COLUMN published_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN published_at SET NOT NULL,
    ADD CONSTRAINT question_sets_review_status_check
      CHECK (review_status IN ('reviewed','pilot','active','retired')),
    ADD CONSTRAINT question_sets_default_target_level_check
      CHECK (default_target_level BETWEEN 1 AND 6),
    ADD CONSTRAINT question_sets_default_predicted_difficulty_check
      CHECK (default_predicted_difficulty BETWEEN -3.0 AND 3.0);

DROP VIEW topik_bank.current_set_contents;

ALTER TABLE topik_bank.question_set_items
    DROP CONSTRAINT question_set_items_set_id_set_version_fkey,
    DROP CONSTRAINT question_set_items_pkey,
    DROP CONSTRAINT question_set_items_set_id_set_version_item_id_key;

DELETE FROM topik_bank.question_set_items member
 USING latest_question_set_versions latest
 WHERE member.set_id=latest.set_id
   AND member.set_version<>latest.set_version;

ALTER TABLE topik_bank.question_set_items
    DROP COLUMN set_version,
    ADD CONSTRAINT question_set_items_pkey PRIMARY KEY (set_id,position),
    ADD CONSTRAINT question_set_items_set_id_item_id_key UNIQUE (set_id,item_id),
    ADD CONSTRAINT question_set_items_set_id_fkey
      FOREIGN KEY (set_id) REFERENCES topik_bank.question_sets(set_id);

DROP TABLE topik_bank.question_set_versions;

CREATE VIEW topik_bank.current_set_contents AS
SELECT question_set.set_id,
       question_set.section AS set_section,
       question_set.generator_provider AS set_generator_provider,
       question_set.generator_model AS set_generator_model,
       question_set.generator_version AS set_generator_version,
       question_set.review_status AS set_review_status,
       question_set.default_target_level,
       question_set.default_predicted_difficulty,
       question_set.published_at,
       member.position,
       item.source_key,
       version.item_id,
       version.item_version,
       version.type_slot,
       version.item_type,
       version.primary_skill,
       version.target_level,
       version.predicted_difficulty,
       version.irt_difficulty,
       version.irt_discrimination,
       version.review_status AS item_review_status,
       version.stem,
       version.choices,
       version.correct_answer,
       version.explanation,
       version.content_json,
       version.source_provenance,
       question_set.set_sequence
  FROM topik_bank.question_sets question_set
  JOIN topik_bank.question_set_items member ON member.set_id=question_set.set_id
  JOIN topik_bank.items item ON item.item_id=member.item_id
  JOIN topik_bank.item_versions version
    ON version.item_id=member.item_id AND version.item_version=member.item_version;
