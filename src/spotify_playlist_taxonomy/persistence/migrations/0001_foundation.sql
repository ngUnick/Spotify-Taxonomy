CREATE TABLE taxonomy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taxonomy_version TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    persisted_at TEXT NOT NULL
);

CREATE TABLE logical_playlists (
    logical_id TEXT PRIMARY KEY,
    dimension TEXT NOT NULL CHECK (dimension IN ('genre', 'vibe', 'activity')),
    created_at TEXT NOT NULL
);

CREATE TABLE logical_playlist_revisions (
    taxonomy_version TEXT NOT NULL REFERENCES taxonomy_versions(taxonomy_version),
    logical_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    specification_status TEXT NOT NULL CHECK (
        specification_status IN ('draft', 'reviewed', 'retired')
    ),
    PRIMARY KEY (taxonomy_version, logical_id)
);

CREATE TABLE specification_versions (
    specification_version TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    logical_playlist_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    digest TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('draft', 'reviewed', 'retired')),
    persisted_at TEXT NOT NULL
);
