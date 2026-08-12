CREATE TABLE IF NOT EXISTS topik_bank.deployment_runs (
    run_id UUID PRIMARY KEY,
    target_fingerprint CHAR(64) NOT NULL,
    target_label TEXT NOT NULL,
    source_snapshot_hash CHAR(64) NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'succeeded', 'rolled_back', 'outcome_unknown')),
    requested_set_count INTEGER NOT NULL CHECK (requested_set_count >= 1),
    transferred_set_count INTEGER NOT NULL DEFAULT 0 CHECK (transferred_set_count >= 0),
    reused_set_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_set_count >= 0),
    created_row_count INTEGER NOT NULL DEFAULT 0 CHECK (created_row_count >= 0),
    reused_row_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_row_count >= 0),
    error_message TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS deployment_runs_started_idx
    ON topik_bank.deployment_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS topik_bank.deployment_run_sets (
    run_id UUID NOT NULL REFERENCES topik_bank.deployment_runs(run_id) ON DELETE CASCADE,
    set_id UUID NOT NULL,
    set_sequence INTEGER NOT NULL CHECK (set_sequence >= 1),
    source_snapshot_hash CHAR(64) NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'succeeded', 'rolled_back', 'outcome_unknown')),
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, set_id)
);
