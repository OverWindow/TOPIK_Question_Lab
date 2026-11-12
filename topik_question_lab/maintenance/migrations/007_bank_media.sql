-- Shared databases must move media with the Web functions in migration 024.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='topik_app' AND tablename IN
    ('tts_audio_assets','item_audio_bindings','item_visual_assets','question_set_item_audio_bindings')) THEN
    RAISE EXCEPTION 'Apply Unigate-PBT-Web 024_bank_media.sql before preparing the shared bank.';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS topik_bank.tts_audio_assets (
  audio_asset_id UUID PRIMARY KEY, source_hash CHAR(64) NOT NULL,
  provider TEXT NOT NULL DEFAULT 'google_cloud_tts', model_name TEXT NOT NULL,
  language_code TEXT NOT NULL DEFAULT 'ko-KR', female_voice TEXT NOT NULL, male_voice TEXT NOT NULL,
  mime_type TEXT NOT NULL DEFAULT 'audio/mpeg', storage_bucket TEXT NOT NULL,
  storage_path TEXT NOT NULL, storage_url TEXT NOT NULL,
  byte_size INTEGER NOT NULL CHECK(byte_size>0), duration_ms INTEGER,
  created_by UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  tts_style JSONB NOT NULL DEFAULT '{"stylePrompt":"","speakingRate":1}', deleted_at TIMESTAMPTZ,
  narration_version TEXT NOT NULL DEFAULT 'dialogue_v1'
    CHECK(narration_version IN ('dialogue_v1','exam_track_v2','exam_track_v3','exam_track_v4')),
  script_snapshot JSONB,
  CHECK(narration_version='dialogue_v1' OR script_snapshot IS NOT NULL),
  UNIQUE(source_hash,model_name,female_voice,male_voice)
);
CREATE TABLE IF NOT EXISTS topik_bank.item_audio_bindings (
  item_id UUID NOT NULL REFERENCES topik_bank.items(item_id), content_hash CHAR(64) NOT NULL,
  audio_asset_id UUID NOT NULL REFERENCES topik_bank.tts_audio_assets(audio_asset_id),
  source_hash CHAR(64) NOT NULL, is_current BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(item_id,content_hash,audio_asset_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS item_audio_current_hash_idx
  ON topik_bank.item_audio_bindings(item_id,content_hash) WHERE is_current;
CREATE TABLE IF NOT EXISTS topik_bank.question_set_item_audio_bindings (
  set_id UUID NOT NULL, position SMALLINT NOT NULL CHECK(position>0),
  audio_asset_id UUID NOT NULL REFERENCES topik_bank.tts_audio_assets(audio_asset_id),
  source_hash CHAR(64) NOT NULL, is_current BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(set_id,position,audio_asset_id),
  FOREIGN KEY(set_id,position) REFERENCES topik_bank.items(set_id,position)
);
CREATE UNIQUE INDEX IF NOT EXISTS question_set_item_audio_current_idx
  ON topik_bank.question_set_item_audio_bindings(set_id,position) WHERE is_current;
CREATE INDEX IF NOT EXISTS question_set_item_audio_asset_idx
  ON topik_bank.question_set_item_audio_bindings(audio_asset_id,is_current);
CREATE TABLE IF NOT EXISTS topik_bank.item_visual_assets (
  visual_asset_id UUID PRIMARY KEY, item_id UUID NOT NULL REFERENCES topik_bank.items(item_id),
  content_hash CHAR(64) NOT NULL, option_number SMALLINT NOT NULL CHECK(option_number BETWEEN 1 AND 4),
  visual_role TEXT NOT NULL DEFAULT 'choice' CHECK(visual_role IN ('choice','material')),
  storage_bucket TEXT NOT NULL, storage_path TEXT NOT NULL, storage_url TEXT NOT NULL,
  mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL CHECK(byte_size>0), created_by UUID,
  is_current BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS item_visual_current_hash_idx
  ON topik_bank.item_visual_assets(item_id,content_hash,visual_role,option_number) WHERE is_current;

-- Creator IDs are provenance; deployment must not require another environment's admin account.
DO $$ DECLARE t TEXT; fk TEXT; BEGIN
  FOREACH t IN ARRAY ARRAY['tts_audio_assets','item_visual_assets'] LOOP
    FOR fk IN SELECT c.conname FROM pg_constraint c
      WHERE c.conrelid=format('topik_bank.%I',t)::regclass AND c.contype='f'
        AND c.confrelid=to_regclass('topik_app.admin_users') LOOP
      EXECUTE format('ALTER TABLE topik_bank.%I DROP CONSTRAINT %I',t,fk);
    END LOOP;
    EXECUTE format('ALTER TABLE topik_bank.%I ADD COLUMN IF NOT EXISTS storage_backend TEXT NOT NULL DEFAULT ''local'' CHECK(storage_backend = ''local'')',t);
    EXECUTE format('ALTER TABLE topik_bank.%I ADD COLUMN IF NOT EXISTS file_sha256 CHAR(64) CHECK(file_sha256 ~ ''^[a-f0-9]{64}$'')',t);
  END LOOP;
END $$;
