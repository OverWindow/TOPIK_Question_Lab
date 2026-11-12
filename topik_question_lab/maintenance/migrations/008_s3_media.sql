-- Extend only the storage discriminator. IDs, bindings and source hashes stay intact.
ALTER TABLE topik_bank.tts_audio_assets
  ALTER COLUMN storage_backend SET DEFAULT 'local';
ALTER TABLE topik_bank.tts_audio_assets
  DROP CONSTRAINT IF EXISTS tts_audio_assets_storage_backend_check;
ALTER TABLE topik_bank.tts_audio_assets
  ADD CONSTRAINT tts_audio_assets_storage_backend_check
  CHECK (storage_backend IN ('local', 's3'));
ALTER TABLE topik_bank.item_visual_assets
  ALTER COLUMN storage_backend SET DEFAULT 'local';
ALTER TABLE topik_bank.item_visual_assets
  DROP CONSTRAINT IF EXISTS item_visual_assets_storage_backend_check;
ALTER TABLE topik_bank.item_visual_assets
  ADD CONSTRAINT item_visual_assets_storage_backend_check
  CHECK (storage_backend IN ('local', 's3'));
