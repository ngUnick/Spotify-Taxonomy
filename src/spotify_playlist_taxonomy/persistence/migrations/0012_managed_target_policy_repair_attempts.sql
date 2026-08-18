CREATE TABLE managed_target_policy_repair_attempts (
    repair_id TEXT PRIMARY KEY,
    output_target_id TEXT NOT NULL,
    registration_id TEXT NOT NULL REFERENCES managed_target_registrations(registration_id),
    state TEXT NOT NULL CHECK (state IN ('no_op', 'started', 'prewrite_failed', 'provider_rejected', 'ambiguous', 'readback_failed', 'policy_mismatch_after_repair', 'confirmed')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX managed_target_policy_repair_by_target
ON managed_target_policy_repair_attempts(output_target_id, created_at);
