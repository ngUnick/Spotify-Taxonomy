CREATE TABLE catalogue_completion_runs (
    run_id TEXT PRIMARY KEY,
    canonical_ids_json TEXT NOT NULL CHECK (json_valid(canonical_ids_json)),
    input_digest TEXT NOT NULL,
    batch_plan_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('started', 'paused_quota', 'recovery_required', 'complete')),
    last_reason TEXT,
    retry_after_seconds INTEGER CHECK (retry_after_seconds IS NULL OR retry_after_seconds >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX catalogue_completion_runs_state_idx
    ON catalogue_completion_runs(state, updated_at DESC);
