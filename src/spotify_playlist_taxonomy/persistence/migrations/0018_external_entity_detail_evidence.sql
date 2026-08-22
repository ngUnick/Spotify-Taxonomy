CREATE TABLE external_entity_detail_evidence (
    evidence_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
    entity_type TEXT NOT NULL CHECK (length(trim(entity_type)) > 0),
    external_id TEXT NOT NULL CHECK (length(trim(external_id)) > 0),
    lookup_state TEXT NOT NULL CHECK (lookup_state IN ('resolved', 'not_found', 'lookup_failed')),
    lookup_method TEXT NOT NULL CHECK (length(trim(lookup_method)) > 0),
    query_fingerprint TEXT NOT NULL CHECK (length(trim(query_fingerprint)) > 0),
    adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) > 0),
    retrieved_at TEXT NOT NULL,
    fresh_until TEXT,
    content_digest TEXT NOT NULL CHECK (length(content_digest) = 64),
    isrcs_available INTEGER NOT NULL CHECK (isrcs_available IN (0, 1)),
    recording_length_ms INTEGER CHECK (recording_length_ms IS NULL OR recording_length_ms >= 0),
    CHECK (lookup_state = 'resolved' OR (isrcs_available = 0 AND recording_length_ms IS NULL)),
    UNIQUE(provider, entity_type, external_id)
);

CREATE TABLE external_entity_detail_isrc_assertions (
    evidence_id TEXT NOT NULL REFERENCES external_entity_detail_evidence(evidence_id) ON DELETE CASCADE,
    normalized_isrc TEXT NOT NULL CHECK (length(trim(normalized_isrc)) > 0),
    PRIMARY KEY(evidence_id, normalized_isrc)
);

CREATE INDEX external_entity_detail_lookup_idx
ON external_entity_detail_evidence(provider, entity_type, external_id, lookup_state);
