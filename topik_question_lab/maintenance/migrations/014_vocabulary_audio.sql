ALTER TABLE topik_bank.vocabulary
    ADD COLUMN IF NOT EXISTS audio_storage_bucket TEXT,
    ADD COLUMN IF NOT EXISTS audio_storage_path TEXT,
    ADD COLUMN IF NOT EXISTS audio_file_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS audio_byte_size INTEGER,
    ADD COLUMN IF NOT EXISTS audio_duration_ms INTEGER,
    ADD COLUMN IF NOT EXISTS audio_source_hash TEXT,
    ADD COLUMN IF NOT EXISTS audio_generation_config JSONB,
    ADD COLUMN IF NOT EXISTS audio_created_at TIMESTAMPTZ;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='topik_bank.vocabulary'::regclass AND conname='vocabulary_audio_complete') THEN
        ALTER TABLE topik_bank.vocabulary ADD CONSTRAINT vocabulary_audio_complete CHECK (
            num_nonnulls(audio_storage_bucket, audio_storage_path, audio_file_sha256,
                audio_byte_size, audio_duration_ms, audio_source_hash,
                audio_generation_config, audio_created_at) = 0
            OR (
                num_nonnulls(audio_storage_bucket, audio_storage_path, audio_file_sha256,
                    audio_byte_size, audio_duration_ms, audio_source_hash,
                    audio_generation_config, audio_created_at) = 8
                AND btrim(audio_storage_bucket) <> '' AND btrim(audio_storage_path) <> ''
                AND audio_file_sha256 ~ '^[a-f0-9]{64}$'
                AND audio_source_hash ~ '^[a-f0-9]{64}$'
                AND audio_byte_size > 0 AND audio_duration_ms > 0
                AND jsonb_typeof(audio_generation_config) = 'object'
            )
        );
    END IF;
END $$;
