-- Preserve published contents; unassigned items keep their latest contents.
LOCK TABLE topik_bank.items, topik_bank.item_versions,
           topik_bank.question_sets, topik_bank.question_set_items
    IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT item_id FROM topik_bank.question_set_items
        GROUP BY item_id HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'A question belongs to multiple sets. Resolve memberships before migration.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM topik_bank.items i
        WHERE NOT EXISTS (SELECT 1 FROM topik_bank.item_versions v WHERE v.item_id=i.item_id)
    ) THEN
        RAISE EXCEPTION 'An item has no contents. Resolve it before migration.';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class r ON r.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE c.confrelid IN ('topik_bank.item_versions'::regclass,
                             'topik_bank.question_set_items'::regclass)
          AND n.nspname<>'topik_bank'
    ) THEN
        RAISE EXCEPTION 'External tables reference question versions or memberships. Migrate the consumer first.';
    END IF;
END $$;

CREATE TEMP TABLE unversioned_view_metadata ON COMMIT DROP AS
SELECT c.oid,n.nspname,c.relname,pg_get_userbyid(c.relowner) AS owner_name,
       c.reloptions,obj_description(c.oid,'pg_class') AS comment,
       COALESCE(c.relacl,acldefault('r',c.relowner)) AS acl
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='topik_bank' AND c.relname IN ('current_items','current_set_contents') AND c.relkind='v';
CREATE TEMP TABLE unversioned_view_column_metadata ON COMMIT DROP AS
SELECT v.nspname,v.relname,a.attname,a.attacl,col_description(a.attrelid,a.attnum) AS comment
FROM unversioned_view_metadata v JOIN pg_attribute a ON a.attrelid=v.oid
WHERE a.attnum>0 AND NOT a.attisdropped AND a.attname NOT IN ('item_version','predicted_difficulty','default_predicted_difficulty','irt_difficulty','irt_discrimination');

CREATE TEMP TABLE collapsed_items ON COMMIT DROP AS
SELECT DISTINCT ON (v.item_id) v.*, m.set_id, m.position
FROM topik_bank.item_versions v
LEFT JOIN topik_bank.question_set_items m ON m.item_id=v.item_id
WHERE m.item_id IS NULL OR m.item_version=v.item_version
ORDER BY v.item_id, v.item_version DESC;

ALTER TABLE topik_bank.items
    ADD COLUMN section TEXT CHECK (section IN ('reading','listening','writing')),
    ADD COLUMN type_slot SMALLINT CHECK (type_slot BETWEEN 1 AND 50),
    ADD COLUMN item_type TEXT,
    ADD COLUMN primary_skill TEXT CHECK (btrim(primary_skill)<>''),
    ADD COLUMN target_level SMALLINT CHECK (target_level BETWEEN 1 AND 6),
    ADD COLUMN stem_length INTEGER CHECK (stem_length>=0),
    ADD COLUMN choice_count INTEGER CHECK (choice_count>=0),
    ADD COLUMN generator_provider TEXT,
    ADD COLUMN generator_model TEXT,
    ADD COLUMN generator_version TEXT,
    ADD COLUMN prompt_version CHAR(64),
    ADD COLUMN review_status TEXT DEFAULT 'reviewed'
        CHECK (review_status IN ('reviewed','pilot','active','retired')),
    ADD COLUMN stem TEXT,
    ADD COLUMN choices JSONB,
    ADD COLUMN correct_answer SMALLINT,
    ADD COLUMN explanation TEXT DEFAULT '',
    ADD COLUMN content_json JSONB,
    ADD COLUMN source_provenance JSONB,
    ADD COLUMN content_hash CHAR(64),
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN set_id UUID REFERENCES topik_bank.question_sets(set_id),
    ADD COLUMN position SMALLINT CHECK (position BETWEEN 1 AND 50),
    ADD CONSTRAINT items_set_position_key UNIQUE (set_id,position),
    ADD CONSTRAINT items_membership_check CHECK ((set_id IS NULL)=(position IS NULL));

UPDATE topik_bank.items i SET
    section=v.section, type_slot=v.type_slot, item_type=v.item_type,
    primary_skill=v.primary_skill, target_level=v.target_level,
    stem_length=v.stem_length,
    choice_count=v.choice_count, generator_provider=v.generator_provider,
    generator_model=v.generator_model, generator_version=v.generator_version,
    prompt_version=v.prompt_version, review_status=v.review_status,
    stem=v.stem, choices=v.choices, correct_answer=v.correct_answer,
    explanation=v.explanation, content_json=v.content_json,
    source_provenance=v.source_provenance || jsonb_build_object('publication_metadata',
        jsonb_build_object('predicted_difficulty',COALESCE(to_jsonb(v)->'predicted_difficulty','0'::jsonb))),
    content_hash=v.content_hash,
    updated_at=v.created_at, set_id=v.set_id, position=v.position
FROM collapsed_items v WHERE v.item_id=i.item_id;

ALTER TABLE topik_bank.items
    ALTER COLUMN section SET NOT NULL,
    ALTER COLUMN type_slot SET NOT NULL,
    ALTER COLUMN item_type SET NOT NULL,
    ALTER COLUMN primary_skill SET NOT NULL,
    ALTER COLUMN target_level SET NOT NULL,
    ALTER COLUMN stem_length SET NOT NULL,
    ALTER COLUMN choice_count SET NOT NULL,
    ALTER COLUMN generator_provider SET NOT NULL,
    ALTER COLUMN generator_model SET NOT NULL,
    ALTER COLUMN generator_version SET NOT NULL,
    ALTER COLUMN prompt_version SET NOT NULL,
    ALTER COLUMN review_status SET NOT NULL,
    ALTER COLUMN stem SET NOT NULL,
    ALTER COLUMN choices SET NOT NULL,
    ALTER COLUMN explanation SET NOT NULL,
    ALTER COLUMN content_json SET NOT NULL,
    ALTER COLUMN source_provenance SET NOT NULL,
    ALTER COLUMN content_hash SET NOT NULL;

DROP VIEW topik_bank.current_items;
DROP VIEW topik_bank.current_set_contents;
DROP TABLE topik_bank.question_set_items;
DROP TABLE topik_bank.item_versions;
ALTER TABLE topik_bank.question_sets DROP COLUMN set_fingerprint;
ALTER TABLE topik_bank.question_sets DROP COLUMN IF EXISTS default_predicted_difficulty;

CREATE INDEX items_lookup_idx ON topik_bank.items(section,item_type,target_level,review_status);
CREATE INDEX items_slot_idx ON topik_bank.items(section,generator_version,type_slot);

CREATE VIEW topik_bank.current_items AS SELECT i.created_at AS item_created_at,i.* FROM topik_bank.items i;
CREATE VIEW topik_bank.current_set_contents AS
SELECT s.set_id, s.section AS set_section,
       s.generator_provider AS set_generator_provider,
       s.generator_model AS set_generator_model,
       s.generator_version AS set_generator_version,
       s.review_status AS set_review_status,
       s.default_target_level, s.published_at,
       i.position, i.source_key, i.item_id, i.content_hash, i.type_slot, i.item_type,
       i.primary_skill, i.target_level, i.review_status AS item_review_status,
       i.stem, i.choices, i.correct_answer, i.explanation,
       i.content_json, i.source_provenance, s.set_sequence
FROM topik_bank.question_sets s JOIN topik_bank.items i ON i.set_id=s.set_id;

-- Restore view ownership and grants; never broaden access after recreation.
DO $$ DECLARE v RECORD; privilege RECORD; col RECORD; recipient TEXT; BEGIN
  FOR v IN SELECT * FROM unversioned_view_metadata LOOP
    FOR privilege IN SELECT DISTINCT a.grantee FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
      WHERE n.nspname=v.nspname AND c.relname=v.relname LOOP
      recipient:=CASE WHEN privilege.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(privilege.grantee)) END;
      EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM %s',v.nspname,v.relname,recipient);
    END LOOP;
    EXECUTE format('ALTER VIEW %I.%I OWNER TO %I',v.nspname,v.relname,v.owner_name);
    IF v.reloptions IS NOT NULL THEN
      EXECUTE format('ALTER VIEW %I.%I SET (%s)',v.nspname,v.relname,array_to_string(v.reloptions,','));
    END IF;
    EXECUTE format('COMMENT ON VIEW %I.%I IS %L',v.nspname,v.relname,v.comment);
    FOR privilege IN SELECT * FROM aclexplode(v.acl) LOOP
      recipient:=CASE WHEN privilege.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(privilege.grantee)) END;
      EXECUTE format('GRANT %s ON TABLE %I.%I TO %s%s',privilege.privilege_type,v.nspname,v.relname,recipient,
        CASE WHEN privilege.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
    END LOOP;
  END LOOP;
  FOR col IN SELECT * FROM unversioned_view_column_metadata LOOP
    EXECUTE format('COMMENT ON COLUMN %I.%I.%I IS %L',col.nspname,col.relname,col.attname,col.comment);
    FOR privilege IN SELECT * FROM aclexplode(col.attacl) LOOP
      recipient:=CASE WHEN privilege.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(privilege.grantee)) END;
      EXECUTE format('GRANT %s (%I) ON TABLE %I.%I TO %s%s',privilege.privilege_type,col.attname,col.nspname,col.relname,recipient,
        CASE WHEN privilege.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
    END LOOP;
  END LOOP;
END $$;

