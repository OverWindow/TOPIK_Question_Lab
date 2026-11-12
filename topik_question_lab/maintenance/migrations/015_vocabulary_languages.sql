ALTER TABLE topik_bank.vocabulary
    ADD COLUMN IF NOT EXISTS chinese_simplified TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS indonesian TEXT NOT NULL DEFAULT '';
