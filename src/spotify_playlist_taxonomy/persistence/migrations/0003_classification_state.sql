CREATE TABLE research_evidence (
    evidence_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    normalized_value TEXT NOT NULL CHECK (length(trim(normalized_value)) > 0),
    source_reference TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE derived_characteristics (
    characteristic_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    value TEXT NOT NULL CHECK (length(trim(value)) > 0),
    origin TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    derivation_version TEXT NOT NULL,
    justification_summary TEXT NOT NULL CHECK (length(justification_summary) <= 280),
    created_at TEXT NOT NULL
);

CREATE TABLE characteristic_evidence (
    characteristic_id TEXT NOT NULL REFERENCES derived_characteristics(characteristic_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES research_evidence(evidence_id),
    PRIMARY KEY (characteristic_id, evidence_id)
);

CREATE TABLE classification_proposals (
    proposal_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    logical_playlist_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'abstain')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    origin TEXT NOT NULL,
    justification_summary TEXT NOT NULL CHECK (length(justification_summary) <= 280),
    taxonomy_version TEXT NOT NULL REFERENCES taxonomy_versions(taxonomy_version),
    specification_version TEXT NOT NULL REFERENCES specification_versions(specification_version),
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE proposal_evidence (
    proposal_id TEXT NOT NULL REFERENCES classification_proposals(proposal_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES research_evidence(evidence_id),
    PRIMARY KEY (proposal_id, evidence_id)
);

CREATE TABLE proposal_characteristics (
    proposal_id TEXT NOT NULL REFERENCES classification_proposals(proposal_id) ON DELETE CASCADE,
    characteristic_id TEXT NOT NULL REFERENCES derived_characteristics(characteristic_id),
    PRIMARY KEY (proposal_id, characteristic_id)
);

CREATE TABLE user_decisions (
    decision_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    logical_playlist_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'abstain')),
    origin TEXT NOT NULL CHECK (origin IN ('user_approval', 'user_override', 'user_explicit')),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (canonical_track_id, logical_playlist_id, sequence)
);

CREATE TABLE calibration_examples (
    calibration_example_id TEXT PRIMARY KEY,
    user_decision_id TEXT NOT NULL UNIQUE REFERENCES user_decisions(decision_id),
    logical_playlist_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    polarity TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    selected_at TEXT NOT NULL
);
