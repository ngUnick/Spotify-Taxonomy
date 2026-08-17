CREATE TABLE research_jobs (
    job_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    request_id TEXT NOT NULL UNIQUE,
    subject_fingerprint TEXT NOT NULL CHECK (length(trim(subject_fingerprint)) > 0),
    research_policy_version TEXT NOT NULL,
    phase4_bundle_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'partial', 'failed')),
    requested_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE research_provider_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query_fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'success', 'no_result', 'ambiguous_subject', 'transient_error', 'unavailable', 'unsupported'
    )),
    cache_hit INTEGER NOT NULL CHECK (cache_hit IN (0, 1)),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    cached_from_attempt_id TEXT REFERENCES research_provider_attempts(attempt_id),
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    retry_after_seconds REAL CHECK (retry_after_seconds IS NULL OR retry_after_seconds >= 0),
    error_category TEXT
);

CREATE TABLE research_provider_assertions (
    assertion_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES research_provider_attempts(attempt_id) ON DELETE CASCADE,
    assertion_key TEXT NOT NULL,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    subject_fingerprint TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    asserted_value_json TEXT NOT NULL CHECK (json_valid(asserted_value_json)),
    evidence_kind TEXT NOT NULL,
    source TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    subject_scope TEXT NOT NULL CHECK (subject_scope IN ('recording', 'work', 'release', 'artist', 'project')),
    subject_match TEXT NOT NULL CHECK (subject_match IN ('exact', 'probable', 'ambiguous', 'contextual')),
    native_confidence REAL CHECK (native_confidence IS NULL OR (native_confidence >= 0 AND native_confidence <= 1)),
    quality_band TEXT NOT NULL CHECK (quality_band IN ('high', 'medium', 'low', 'unknown')),
    observed_at TEXT,
    retrieved_at TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    UNIQUE (attempt_id, assertion_key)
);

CREATE TABLE research_cache_entries (
    cache_key TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    request_schema_version TEXT NOT NULL,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    subject_fingerprint TEXT NOT NULL,
    query_fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'no_result', 'ambiguous_subject', 'unsupported')),
    producing_attempt_id TEXT NOT NULL REFERENCES research_provider_attempts(attempt_id),
    created_at TEXT NOT NULL,
    fresh_until TEXT,
    negative_until TEXT,
    invalidated_at TEXT,
    invalidation_reason TEXT
);

ALTER TABLE research_evidence ADD COLUMN provider_assertion_id TEXT REFERENCES research_provider_assertions(assertion_id);
ALTER TABLE research_evidence ADD COLUMN provider_id TEXT;
ALTER TABLE research_evidence ADD COLUMN provider_version TEXT;
ALTER TABLE research_evidence ADD COLUMN request_id TEXT;
ALTER TABLE research_evidence ADD COLUMN query_id TEXT;
ALTER TABLE research_evidence ADD COLUMN subject_fingerprint TEXT;
ALTER TABLE research_evidence ADD COLUMN subject_scope TEXT;
ALTER TABLE research_evidence ADD COLUMN subject_match TEXT;
ALTER TABLE research_evidence ADD COLUMN evidence_type TEXT;
ALTER TABLE research_evidence ADD COLUMN claim_key TEXT;
ALTER TABLE research_evidence ADD COLUMN quality_band TEXT;
ALTER TABLE research_evidence ADD COLUMN normalized_schema_version TEXT;
ALTER TABLE research_evidence ADD COLUMN normalizer_version TEXT;
ALTER TABLE research_evidence ADD COLUMN retrieved_at TEXT;
ALTER TABLE research_evidence ADD COLUMN fresh_until TEXT;
ALTER TABLE research_evidence ADD COLUMN evidence_state TEXT NOT NULL DEFAULT 'active' CHECK (evidence_state IN ('active', 'stale', 'invalidated'));
ALTER TABLE research_evidence ADD COLUMN content_digest TEXT;
ALTER TABLE research_evidence ADD COLUMN invalidated_at TEXT;
ALTER TABLE research_evidence ADD COLUMN invalidation_reason TEXT;

CREATE INDEX research_jobs_track_idx ON research_jobs(canonical_track_id, requested_at);
CREATE INDEX research_attempts_job_provider_idx ON research_provider_attempts(job_id, provider_id);
CREATE INDEX research_assertions_track_type_idx ON research_provider_assertions(canonical_track_id, evidence_type);
CREATE INDEX research_evidence_track_type_idx ON research_evidence(canonical_track_id, evidence_type, evidence_state);
CREATE INDEX research_cache_lookup_idx ON research_cache_entries(provider_id, canonical_track_id, subject_fingerprint);
