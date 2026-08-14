CREATE TABLE canonical_tracks (
    canonical_track_id TEXT PRIMARY KEY,
    display_title TEXT NOT NULL CHECK (length(trim(display_title)) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE canonical_track_artists (
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    artist_name TEXT NOT NULL CHECK (length(trim(artist_name)) > 0),
    PRIMARY KEY (canonical_track_id, position)
);

CREATE TABLE track_identities (
    identity_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_track_id TEXT,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    normalized_title TEXT NOT NULL,
    version_label TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (provider, provider_track_id)
);

CREATE TABLE track_identity_artists (
    identity_id TEXT NOT NULL REFERENCES track_identities(identity_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    artist_name TEXT NOT NULL CHECK (length(trim(artist_name)) > 0),
    normalized_artist_name TEXT NOT NULL,
    PRIMARY KEY (identity_id, position)
);

CREATE TABLE canonical_track_identity_links (
    link_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    identity_id TEXT NOT NULL UNIQUE REFERENCES track_identities(identity_id),
    linked_at TEXT NOT NULL
);

CREATE TABLE track_match_assessments (
    assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_identity_id TEXT NOT NULL REFERENCES track_identities(identity_id),
    right_identity_id TEXT NOT NULL REFERENCES track_identities(identity_id),
    state TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    basis_json TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    CHECK (left_identity_id <> right_identity_id)
);

CREATE TABLE platform_playlist_registrations (
    registration_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_playlist_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('source', 'inbox', 'managed')),
    logical_playlist_id TEXT REFERENCES logical_playlists(logical_id),
    owner_account_id TEXT,
    display_name TEXT,
    registered_at TEXT NOT NULL,
    UNIQUE (provider, provider_playlist_id)
);
