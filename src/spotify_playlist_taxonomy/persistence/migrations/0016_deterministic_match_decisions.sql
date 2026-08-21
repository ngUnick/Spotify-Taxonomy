CREATE TABLE deterministic_match_decisions (
    decision_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    account_binding_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES canonical_match_candidates(candidate_id),
    identity_id TEXT NOT NULL,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    link_id TEXT NOT NULL REFERENCES account_canonical_identity_links(link_id),
    link_event_id TEXT NOT NULL REFERENCES canonical_identity_link_events(event_id),
    comparison_count INTEGER NOT NULL CHECK (comparison_count >= 1),
    duration_threshold_ms INTEGER NOT NULL CHECK (duration_threshold_ms = 500),
    evidence_summary TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(candidate_id)
);

CREATE INDEX deterministic_match_decisions_by_account_policy
ON deterministic_match_decisions(account_binding_id, policy_id, decided_at, decision_id);
