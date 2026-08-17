CREATE TABLE phase6_characteristic_derivations (
    derivation_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    characteristic_name TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    value_type TEXT NOT NULL,
    origin TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    derivation_version TEXT NOT NULL,
    vocabulary_version TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('current', 'conflicting', 'superseded')),
    justification_summary TEXT NOT NULL CHECK (length(justification_summary) <= 280),
    created_at TEXT NOT NULL,
    UNIQUE(canonical_track_id, characteristic_name, derivation_version, input_digest)
);
CREATE TABLE phase6_characteristic_evidence (
    derivation_id TEXT NOT NULL REFERENCES phase6_characteristic_derivations(derivation_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES research_evidence(evidence_id),
    PRIMARY KEY(derivation_id, evidence_id)
);
CREATE UNIQUE INDEX phase6_current_characteristic_idx ON phase6_characteristic_derivations(canonical_track_id, characteristic_name) WHERE state = 'current';
CREATE TABLE phase6_proposals (
    proposal_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    logical_playlist_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'abstain')),
    proposal_state TEXT NOT NULL CHECK (proposal_state IN ('supported', 'insufficient', 'conflicting', 'hard_excluded')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    specification_version TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    vocabulary_version TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    review_status TEXT NOT NULL,
    explanation_json TEXT NOT NULL CHECK (json_valid(explanation_json)),
    created_at TEXT NOT NULL,
    UNIQUE(canonical_track_id, logical_playlist_id, evaluator_version, input_digest)
);
CREATE TABLE phase6_proposal_derivations (
    proposal_id TEXT NOT NULL REFERENCES phase6_proposals(proposal_id) ON DELETE CASCADE,
    derivation_id TEXT NOT NULL REFERENCES phase6_characteristic_derivations(derivation_id),
    PRIMARY KEY(proposal_id, derivation_id)
);
