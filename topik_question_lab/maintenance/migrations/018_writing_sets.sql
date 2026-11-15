CREATE TABLE topik_bank.writing_sets (
    set_id UUID PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE CHECK (btrim(source_key) <> ''),
    exam_format TEXT NOT NULL DEFAULT 'pbt' CHECK (exam_format IN ('pbt', 'ibt')),
    provider TEXT NOT NULL CHECK (btrim(provider) <> ''),
    model TEXT NOT NULL CHECK (btrim(model) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE topik_bank.writing_questions (
    set_id UUID NOT NULL REFERENCES topik_bank.writing_sets(set_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    type_slot SMALLINT NOT NULL CHECK (type_slot IN (51, 52, 53, 54)),
    source_generated_id BIGINT NOT NULL CHECK (source_generated_id > 0),
    content_json JSONB NOT NULL CHECK (jsonb_typeof(content_json) = 'object'),
    PRIMARY KEY (set_id, type_slot),
    CHECK (COALESCE(content_json->'type_slot' = to_jsonb(type_slot), FALSE))
);
