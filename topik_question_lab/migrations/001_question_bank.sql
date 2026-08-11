CREATE TABLE IF NOT EXISTS topik_bank.items (
    item_id UUID PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS topik_bank.item_versions (
    item_id UUID NOT NULL REFERENCES topik_bank.items(item_id),
    item_version INTEGER NOT NULL CHECK (item_version >= 1),
    section TEXT NOT NULL CHECK (section IN ('reading', 'listening', 'writing')),
    item_type TEXT NOT NULL,
    primary_skill TEXT NOT NULL CHECK (btrim(primary_skill) <> ''),
    target_level SMALLINT NOT NULL CHECK (target_level BETWEEN 1 AND 6),
    predicted_difficulty DOUBLE PRECISION NOT NULL CHECK (predicted_difficulty BETWEEN -3.0 AND 3.0),
    irt_difficulty DOUBLE PRECISION,
    irt_discrimination DOUBLE PRECISION CHECK (irt_discrimination IS NULL OR irt_discrimination > 0),
    stem_length INTEGER NOT NULL CHECK (stem_length >= 0),
    choice_count INTEGER NOT NULL CHECK (choice_count >= 0),
    generator_provider TEXT NOT NULL,
    generator_model TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    prompt_version CHAR(64) NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'reviewed'
        CHECK (review_status IN ('reviewed', 'pilot', 'active', 'retired')),
    stem TEXT NOT NULL,
    choices JSONB NOT NULL,
    correct_answer SMALLINT,
    explanation TEXT NOT NULL DEFAULT '',
    content_json JSONB NOT NULL,
    source_provenance JSONB NOT NULL,
    content_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (item_id, item_version),
    UNIQUE (item_id, content_hash)
);

CREATE INDEX IF NOT EXISTS item_versions_lookup_idx
    ON topik_bank.item_versions(section, item_type, target_level, review_status);
CREATE INDEX IF NOT EXISTS item_versions_generator_idx
    ON topik_bank.item_versions(generator_provider, generator_version);

CREATE TABLE IF NOT EXISTS topik_bank.question_sets (
    set_id UUID PRIMARY KEY,
    section TEXT NOT NULL CHECK (section IN ('reading', 'listening', 'writing')),
    generator_provider TEXT NOT NULL,
    generator_model TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (section, generator_provider, generator_model, generator_version)
);

CREATE TABLE IF NOT EXISTS topik_bank.question_set_versions (
    set_id UUID NOT NULL REFERENCES topik_bank.question_sets(set_id),
    set_version INTEGER NOT NULL CHECK (set_version >= 1),
    review_status TEXT NOT NULL DEFAULT 'reviewed'
        CHECK (review_status IN ('reviewed', 'pilot', 'active', 'retired')),
    default_target_level SMALLINT NOT NULL CHECK (default_target_level BETWEEN 1 AND 6),
    default_predicted_difficulty DOUBLE PRECISION NOT NULL
        CHECK (default_predicted_difficulty BETWEEN -3.0 AND 3.0),
    set_fingerprint CHAR(64) NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (set_id, set_version),
    UNIQUE (set_id, set_fingerprint)
);

CREATE TABLE IF NOT EXISTS topik_bank.question_set_items (
    set_id UUID NOT NULL,
    set_version INTEGER NOT NULL,
    position SMALLINT NOT NULL CHECK (position BETWEEN 1 AND 50),
    item_id UUID NOT NULL,
    item_version INTEGER NOT NULL,
    PRIMARY KEY (set_id, set_version, position),
    UNIQUE (set_id, set_version, item_id),
    FOREIGN KEY (set_id, set_version)
        REFERENCES topik_bank.question_set_versions(set_id, set_version),
    FOREIGN KEY (item_id, item_version)
        REFERENCES topik_bank.item_versions(item_id, item_version)
);

