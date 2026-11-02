-- Empty consumer databases only. Authoring/deployment history is not transferred.
CREATE SCHEMA IF NOT EXISTS topik_bank;

CREATE TABLE topik_bank.question_sets (
    set_id UUID PRIMARY KEY,
    section TEXT NOT NULL CHECK (section IN ('reading','listening','writing')),
    generator_provider TEXT NOT NULL, generator_model TEXT NOT NULL, generator_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    set_sequence INTEGER NOT NULL CHECK (set_sequence >= 1),
    review_status TEXT NOT NULL DEFAULT 'reviewed'
        CHECK (review_status IN ('pending_review','reviewed','pilot','active','retired')),
    default_target_level SMALLINT NOT NULL CHECK (default_target_level BETWEEN 1 AND 6),
    published_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (section,generator_provider,generator_model,generator_version,set_sequence)
);
CREATE TABLE topik_bank.items (
    item_id UUID PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    section TEXT NOT NULL CHECK (section IN ('reading','listening','writing')),
    type_slot SMALLINT NOT NULL CHECK (type_slot BETWEEN 1 AND 50), item_type TEXT NOT NULL,
    primary_skill TEXT NOT NULL CHECK (btrim(primary_skill) <> ''),
    target_level SMALLINT NOT NULL CHECK (target_level BETWEEN 1 AND 6),
    stem_length INTEGER NOT NULL CHECK (stem_length >= 0),
    choice_count INTEGER NOT NULL CHECK (choice_count >= 0),
    generator_provider TEXT NOT NULL, generator_model TEXT NOT NULL, generator_version TEXT NOT NULL,
    prompt_version CHAR(64) NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'reviewed'
        CHECK (review_status IN ('pending_review','reviewed','pilot','active','retired')),
    stem TEXT NOT NULL, choices JSONB NOT NULL, correct_answer SMALLINT,
    explanation TEXT NOT NULL DEFAULT '', content_json JSONB NOT NULL,
    source_provenance JSONB NOT NULL, content_hash CHAR(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    set_id UUID REFERENCES topik_bank.question_sets(set_id),
    position SMALLINT CHECK (position BETWEEN 1 AND 50),
    UNIQUE (set_id,position), CHECK ((set_id IS NULL) = (position IS NULL))
);
CREATE INDEX items_lookup_idx ON topik_bank.items(section,item_type,target_level,review_status);
CREATE INDEX items_slot_idx ON topik_bank.items(section,generator_version,type_slot);

CREATE TABLE topik_bank.tts_audio_assets (
    audio_asset_id UUID PRIMARY KEY, source_hash CHAR(64) NOT NULL,
    provider TEXT NOT NULL DEFAULT 'google_cloud_tts', model_name TEXT NOT NULL,
    language_code TEXT NOT NULL DEFAULT 'ko-KR', female_voice TEXT NOT NULL, male_voice TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'audio/mpeg', storage_bucket TEXT NOT NULL,
    storage_path TEXT NOT NULL, storage_url TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size > 0), duration_ms INTEGER,
    created_by UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tts_style JSONB NOT NULL DEFAULT '{"stylePrompt":"","speakingRate":1}', deleted_at TIMESTAMPTZ,
    narration_version TEXT NOT NULL DEFAULT 'dialogue_v1'
        CHECK (narration_version IN ('dialogue_v1','exam_track_v2','exam_track_v3','exam_track_v4')),
    script_snapshot JSONB,
    CHECK (narration_version = 'dialogue_v1' OR script_snapshot IS NOT NULL),
    UNIQUE (source_hash,model_name,female_voice,male_voice),
    storage_backend TEXT NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local','supabase','s3')),
    file_sha256 CHAR(64) CHECK (file_sha256 ~ '^[a-f0-9]{64}$')
);
CREATE TABLE topik_bank.item_audio_bindings (
    item_id UUID NOT NULL REFERENCES topik_bank.items(item_id), content_hash CHAR(64) NOT NULL,
    audio_asset_id UUID NOT NULL REFERENCES topik_bank.tts_audio_assets(audio_asset_id),
    source_hash CHAR(64) NOT NULL, is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (item_id,content_hash,audio_asset_id)
);
CREATE UNIQUE INDEX item_audio_current_hash_idx ON topik_bank.item_audio_bindings(item_id,content_hash) WHERE is_current;
CREATE TABLE topik_bank.question_set_item_audio_bindings (
    set_id UUID NOT NULL, position SMALLINT NOT NULL CHECK (position > 0),
    audio_asset_id UUID NOT NULL REFERENCES topik_bank.tts_audio_assets(audio_asset_id),
    source_hash CHAR(64) NOT NULL, is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (set_id,position,audio_asset_id),
    FOREIGN KEY (set_id,position) REFERENCES topik_bank.items(set_id,position)
);
CREATE UNIQUE INDEX question_set_item_audio_current_idx ON topik_bank.question_set_item_audio_bindings(set_id,position) WHERE is_current;
CREATE INDEX question_set_item_audio_asset_idx ON topik_bank.question_set_item_audio_bindings(audio_asset_id,is_current);
CREATE TABLE topik_bank.item_visual_assets (
    visual_asset_id UUID PRIMARY KEY, item_id UUID NOT NULL REFERENCES topik_bank.items(item_id),
    content_hash CHAR(64) NOT NULL, option_number SMALLINT NOT NULL CHECK (option_number BETWEEN 1 AND 4),
    visual_role TEXT NOT NULL DEFAULT 'choice' CHECK (visual_role IN ('choice','material')),
    storage_bucket TEXT NOT NULL, storage_path TEXT NOT NULL, storage_url TEXT NOT NULL,
    mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL CHECK (byte_size > 0), created_by UUID,
    is_current BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    storage_backend TEXT NOT NULL DEFAULT 'local' CHECK (storage_backend IN ('local','supabase','s3')),
    file_sha256 CHAR(64) CHECK (file_sha256 ~ '^[a-f0-9]{64}$')
);
CREATE UNIQUE INDEX item_visual_current_hash_idx ON topik_bank.item_visual_assets(item_id,content_hash,visual_role,option_number) WHERE is_current;

CREATE VIEW topik_bank.current_items AS SELECT i.created_at AS item_created_at,i.* FROM topik_bank.items i;
CREATE VIEW topik_bank.current_set_contents AS
SELECT s.set_id,s.section AS set_section,s.generator_provider AS set_generator_provider,
       s.generator_model AS set_generator_model,s.generator_version AS set_generator_version,
       s.review_status AS set_review_status,s.default_target_level,s.published_at,
       i.position,i.source_key,i.item_id,i.content_hash,i.type_slot,i.item_type,i.primary_skill,
       i.target_level,i.review_status AS item_review_status,i.stem,i.choices,i.correct_answer,
       i.explanation,i.content_json,i.source_provenance,s.set_sequence
FROM topik_bank.question_sets s JOIN topik_bank.items i ON i.set_id=s.set_id;
