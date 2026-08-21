CREATE TABLE identity_external_entity_evidence (
    evidence_id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES track_identities(identity_id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
    entity_type TEXT NOT NULL CHECK (length(trim(entity_type)) > 0),
    external_id TEXT,
    lookup_state TEXT NOT NULL CHECK (lookup_state IN ('resolved', 'ambiguous', 'not_found', 'insufficient_input', 'lookup_failed', 'stale')),
    lookup_method TEXT NOT NULL CHECK (length(trim(lookup_method)) > 0),
    query_fingerprint TEXT NOT NULL CHECK (length(trim(query_fingerprint)) > 0),
    adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) > 0),
    retrieved_at TEXT NOT NULL,
    fresh_until TEXT,
    ambiguity_count INTEGER CHECK (ambiguity_count IS NULL OR ambiguity_count >= 0),
    ambiguity_digest TEXT,
    CHECK ((lookup_state = 'resolved' AND external_id IS NOT NULL AND length(trim(external_id)) > 0) OR (lookup_state <> 'resolved' AND external_id IS NULL)),
    UNIQUE(identity_id, provider, entity_type)
);
CREATE INDEX identity_external_entity_evidence_lookup_idx
ON identity_external_entity_evidence(identity_id, provider, entity_type, lookup_state);
