CREATE TABLE codex_batches (
    batch_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('awaiting_result', 'imported', 'stale', 'superseded')),
    outbound_digest TEXT NOT NULL UNIQUE,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    outbound_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE codex_batch_items (
    batch_id TEXT NOT NULL REFERENCES codex_batches(batch_id),
    item_id TEXT NOT NULL,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    context_json TEXT NOT NULL CHECK (json_valid(context_json)),
    PRIMARY KEY (batch_id, item_id),
    UNIQUE (batch_id, canonical_track_id)
);

CREATE TABLE codex_batch_imports (
    import_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE REFERENCES codex_batches(batch_id),
    result_schema_version TEXT NOT NULL,
    result_digest TEXT NOT NULL UNIQUE,
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    proposal_count INTEGER NOT NULL CHECK (proposal_count >= 0),
    imported_at TEXT NOT NULL
);

CREATE TABLE phase8_codex_proposals (
    proposal_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES codex_batch_imports(import_id),
    batch_id TEXT NOT NULL REFERENCES codex_batches(batch_id),
    item_id TEXT NOT NULL,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    logical_playlist_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    decision TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'abstain')),
    taxonomy_version TEXT NOT NULL,
    specification_version TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    justification_summary TEXT NOT NULL CHECK (length(justification_summary) <= 280),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, item_id, logical_playlist_id)
);

CREATE TABLE phase8_codex_proposal_evidence (
    proposal_id TEXT NOT NULL REFERENCES phase8_codex_proposals(proposal_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES research_evidence(evidence_id),
    PRIMARY KEY (proposal_id, evidence_id)
);

CREATE TABLE phase8_codex_proposal_characteristics (
    proposal_id TEXT NOT NULL REFERENCES phase8_codex_proposals(proposal_id) ON DELETE CASCADE,
    derivation_id TEXT NOT NULL REFERENCES phase6_characteristic_derivations(derivation_id),
    PRIMARY KEY (proposal_id, derivation_id)
);

CREATE INDEX phase8_codex_proposals_scope_idx
    ON phase8_codex_proposals(canonical_track_id, logical_playlist_id, created_at);
