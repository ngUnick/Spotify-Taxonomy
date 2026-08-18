CREATE TABLE managed_target_addition_attempts (
    attempt_id TEXT PRIMARY KEY,
    output_target_id TEXT NOT NULL REFERENCES managed_target_registrations(output_target_id),
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    identity_id TEXT NOT NULL REFERENCES track_identities(identity_id),
    registration_id TEXT NOT NULL REFERENCES managed_target_registrations(registration_id),
    state TEXT NOT NULL CHECK (state IN ('started', 'request_not_sent', 'provider_rejected', 'recovery_required', 'confirmed_added', 'confirmed_recovered')),
    confirmation TEXT NOT NULL,
    provider_acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (provider_acknowledged IN (0, 1)),
    readback_membership_count INTEGER CHECK (readback_membership_count >= 0),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX managed_target_addition_attempts_by_target_track
ON managed_target_addition_attempts(output_target_id, canonical_track_id, created_at);

CREATE INDEX managed_target_addition_attempts_unresolved
ON managed_target_addition_attempts(output_target_id, canonical_track_id)
WHERE state IN ('started', 'recovery_required');
