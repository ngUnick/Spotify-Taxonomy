CREATE TABLE managed_target_abandoned_unknown_attempts (
    history_id TEXT PRIMARY KEY,
    output_target_id TEXT NOT NULL,
    registration_id TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK (provider = 'spotify'),
    account_binding_id TEXT NOT NULL REFERENCES platform_account_bindings(binding_id),
    provider_playlist_id TEXT CHECK (provider_playlist_id IS NULL),
    original_state TEXT NOT NULL CHECK (original_state = 'recovery_required'),
    outcome_state TEXT NOT NULL CHECK (outcome_state = 'abandoned_unknown'),
    provenance TEXT NOT NULL CHECK (provenance = 'operator_abandon_unknown.v1'),
    abandoned_at TEXT NOT NULL,
    confirmation TEXT NOT NULL
);

CREATE INDEX managed_target_abandoned_unknown_by_target
ON managed_target_abandoned_unknown_attempts(output_target_id, abandoned_at);
