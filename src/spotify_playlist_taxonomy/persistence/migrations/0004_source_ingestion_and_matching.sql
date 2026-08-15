CREATE TABLE platform_account_bindings (
    binding_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider = 'spotify'),
    account_id TEXT NOT NULL UNIQUE,
    current_user_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    bound_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    unbound_at TEXT,
    CHECK ((active = 1 AND unbound_at IS NULL) OR active = 0)
);

CREATE UNIQUE INDEX one_active_platform_account
ON platform_account_bindings(provider)
WHERE active = 1;

ALTER TABLE platform_playlist_registrations
    ADD COLUMN account_binding_id TEXT REFERENCES platform_account_bindings(binding_id);
ALTER TABLE platform_playlist_registrations
    ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1));
ALTER TABLE platform_playlist_registrations
    ADD COLUMN disabled_at TEXT;

CREATE UNIQUE INDEX playlist_registration_account_pair
ON platform_playlist_registrations(registration_id, account_binding_id);

CREATE TABLE ingestion_sources (
    source_id TEXT PRIMARY KEY,
    account_binding_id TEXT NOT NULL REFERENCES platform_account_bindings(binding_id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('playlist', 'saved_tracks')),
    playlist_registration_id TEXT UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('source', 'inbox')),
    inbox_kind TEXT CHECK (inbox_kind IN ('liked_songs', 'shazam')),
    display_label TEXT,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    registered_at TEXT NOT NULL,
    disabled_at TEXT,
    FOREIGN KEY (playlist_registration_id, account_binding_id)
        REFERENCES platform_playlist_registrations(registration_id, account_binding_id),
    CHECK (
        (
            source_kind = 'saved_tracks'
            AND playlist_registration_id IS NULL
            AND role = 'inbox'
            AND inbox_kind = 'liked_songs'
        )
        OR
        (
            source_kind = 'playlist'
            AND playlist_registration_id IS NOT NULL
            AND (inbox_kind IS NULL OR inbox_kind = 'shazam')
            AND (inbox_kind IS NULL OR role = 'inbox')
        )
    ),
    CHECK (
        (active = 1 AND disabled_at IS NULL)
        OR (active = 0 AND disabled_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX one_active_saved_tracks_source
ON ingestion_sources(account_binding_id)
WHERE active = 1 AND source_kind = 'saved_tracks';

CREATE UNIQUE INDEX one_active_shazam_source
ON ingestion_sources(account_binding_id)
WHERE active = 1 AND inbox_kind = 'shazam';

CREATE INDEX ingestion_sources_by_account
ON ingestion_sources(account_binding_id, active, registered_at);

CREATE UNIQUE INDEX ingestion_source_account_pair
ON ingestion_sources(source_id, account_binding_id);

CREATE TABLE platform_playlist_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_binding_id TEXT NOT NULL,
    registration_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    ownership_state TEXT NOT NULL CHECK (ownership_state IN ('owned', 'non_owned', 'unknown')),
    collaborative INTEGER NOT NULL CHECK (collaborative IN (0, 1)),
    public INTEGER CHECK (public IS NULL OR public IN (0, 1)),
    item_count INTEGER CHECK (item_count IS NULL OR item_count >= 0),
    snapshot_id TEXT,
    accessibility_state TEXT NOT NULL CHECK (
        accessibility_state IN ('unknown', 'accessible', 'forbidden', 'not_found')
    ),
    observed_at TEXT NOT NULL,
    FOREIGN KEY (registration_id, account_binding_id)
        REFERENCES platform_playlist_registrations(registration_id, account_binding_id)
);

CREATE INDEX playlist_observations_by_registration
ON platform_playlist_observations(registration_id, observed_at);

CREATE TABLE source_scans (
    scan_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    account_binding_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'stale', 'abandoned')
    ),
    starting_snapshot_id TEXT,
    ending_snapshot_id TEXT,
    resume_cursor TEXT,
    next_offset INTEGER NOT NULL DEFAULT 0 CHECK (next_offset >= 0),
    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
    issue_count INTEGER NOT NULL DEFAULT 0 CHECK (issue_count >= 0),
    starting_total INTEGER CHECK (starting_total IS NULL OR starting_total >= 0),
    initial_page_signature TEXT,
    enumeration_complete INTEGER NOT NULL DEFAULT 0 CHECK (enumeration_complete IN (0, 1)),
    content_digest TEXT,
    digest_version TEXT NOT NULL,
    failure_kind TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (scan_id, source_id),
    UNIQUE (scan_id, source_id, account_binding_id),
    FOREIGN KEY (source_id, account_binding_id)
        REFERENCES ingestion_sources(source_id, account_binding_id),
    CHECK (
        status <> 'completed'
        OR (
            enumeration_complete = 1
            AND resume_cursor IS NULL
            AND content_digest IS NOT NULL
            AND completed_at IS NOT NULL
        )
    ),
    CHECK (status NOT IN ('stale', 'abandoned') OR completed_at IS NOT NULL)
);

CREATE INDEX source_scans_by_source_status
ON source_scans(source_id, status, started_at);

CREATE TABLE source_scan_entries (
    scan_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_position INTEGER NOT NULL CHECK (source_position >= 0),
    page_ordinal INTEGER NOT NULL CHECK (page_ordinal >= 0),
    identity_id TEXT REFERENCES track_identities(identity_id),
    issue_kind TEXT CHECK (issue_kind IN ('unavailable', 'local', 'unsupported', 'null', 'malformed')),
    PRIMARY KEY (scan_id, source_position),
    FOREIGN KEY (scan_id, source_id) REFERENCES source_scans(scan_id, source_id) ON DELETE CASCADE,
    CHECK ((identity_id IS NOT NULL) <> (issue_kind IS NOT NULL))
);

CREATE INDEX source_scan_entries_by_identity
ON source_scan_entries(identity_id, scan_id);

CREATE TABLE source_publications (
    source_id TEXT PRIMARY KEY,
    account_binding_id TEXT NOT NULL,
    scan_id TEXT NOT NULL UNIQUE,
    published_at TEXT NOT NULL,
    FOREIGN KEY (source_id, account_binding_id)
        REFERENCES ingestion_sources(source_id, account_binding_id),
    FOREIGN KEY (scan_id, source_id, account_binding_id)
        REFERENCES source_scans(scan_id, source_id, account_binding_id)
);

CREATE TABLE account_track_identity_state (
    account_binding_id TEXT NOT NULL REFERENCES platform_account_bindings(binding_id),
    identity_id TEXT NOT NULL REFERENCES track_identities(identity_id) ON DELETE CASCADE,
    first_seen_scan_id TEXT NOT NULL,
    first_seen_source_id TEXT NOT NULL,
    first_published_scan_id TEXT,
    first_published_source_id TEXT,
    preexisting INTEGER NOT NULL CHECK (preexisting IN (0, 1)),
    matching_touched INTEGER NOT NULL DEFAULT 0 CHECK (matching_touched IN (0, 1)),
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (account_binding_id, identity_id),
    FOREIGN KEY (first_seen_scan_id, first_seen_source_id, account_binding_id)
        REFERENCES source_scans(scan_id, source_id, account_binding_id),
    FOREIGN KEY (first_published_scan_id, first_published_source_id, account_binding_id)
        REFERENCES source_scans(scan_id, source_id, account_binding_id),
    CHECK (
        (first_published_scan_id IS NULL AND first_published_source_id IS NULL)
        OR (first_published_scan_id IS NOT NULL AND first_published_source_id IS NOT NULL)
    )
);

CREATE INDEX account_identity_state_by_identity
ON account_track_identity_state(identity_id, account_binding_id);

CREATE TABLE account_canonical_identity_links (
    link_id TEXT PRIMARY KEY,
    account_binding_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    linked_at TEXT NOT NULL,
    UNIQUE (account_binding_id, identity_id),
    FOREIGN KEY (account_binding_id, identity_id)
        REFERENCES account_track_identity_state(account_binding_id, identity_id)
);

CREATE INDEX account_links_by_canonical
ON account_canonical_identity_links(account_binding_id, canonical_track_id);

CREATE TABLE canonical_match_candidates (
    candidate_id TEXT PRIMARY KEY,
    account_binding_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    canonical_track_id TEXT REFERENCES canonical_tracks(canonical_track_id),
    action TEXT NOT NULL CHECK (action IN ('link_existing', 'create_new')),
    state TEXT NOT NULL CHECK (
        state IN ('candidate', 'ambiguous', 'user_confirmation_required', 'confirmed', 'rejected', 'superseded')
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    basis_json TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    decision_origin TEXT CHECK (
        decision_origin IS NULL OR decision_origin IN (
            'user_confirmation', 'user_create_new', 'user_rejection'
        )
    ),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE (account_binding_id, identity_id, matcher_version, candidate_key),
    FOREIGN KEY (account_binding_id, identity_id)
        REFERENCES account_track_identity_state(account_binding_id, identity_id),
    CHECK (
        (action = 'create_new' AND canonical_track_id IS NULL)
        OR (action = 'link_existing' AND canonical_track_id IS NOT NULL)
    )
);

CREATE INDEX match_candidates_by_account_state
ON canonical_match_candidates(account_binding_id, state, created_at);

CREATE INDEX match_candidates_by_identity
ON canonical_match_candidates(account_binding_id, identity_id, state);

CREATE TABLE canonical_identity_link_events (
    event_id TEXT PRIMARY KEY,
    account_binding_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    link_id TEXT,
    action TEXT NOT NULL CHECK (action IN ('link', 'unlink', 'relink', 'create_new')),
    origin TEXT NOT NULL CHECK (
        origin IN ('system_bootstrap', 'user_confirmation', 'user_create_new', 'user_unlink')
    ),
    candidate_id TEXT REFERENCES canonical_match_candidates(candidate_id),
    event_at TEXT NOT NULL,
    FOREIGN KEY (account_binding_id, identity_id)
        REFERENCES account_track_identity_state(account_binding_id, identity_id)
);

CREATE INDEX link_events_by_account_identity
ON canonical_identity_link_events(account_binding_id, identity_id, event_at);
