-- Legacy audio cleanup requires an explicit review and backup, never an automatic delete.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM topik_bank.tts_audio_assets WHERE narration_version <> 'exam_track_v4') THEN
        RAISE EXCEPTION 'Legacy audio remains. Back up and explicitly review its bindings before applying 016_exam_track_v4_only.';
    END IF;
END $$;

ALTER TABLE topik_bank.tts_audio_assets
    ALTER COLUMN narration_version SET DEFAULT 'exam_track_v4',
    DROP CONSTRAINT IF EXISTS tts_audio_assets_narration_version_check,
    DROP CONSTRAINT IF EXISTS tts_audio_assets_check,
    DROP CONSTRAINT IF EXISTS tts_audio_assets_script_snapshot_check,
    ADD CONSTRAINT tts_audio_assets_narration_version_check CHECK (narration_version = 'exam_track_v4'),
    ALTER COLUMN script_snapshot SET NOT NULL;
