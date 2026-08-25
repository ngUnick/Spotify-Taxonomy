CREATE TABLE genre_routing_events (
    routing_event_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    route TEXT NOT NULL CHECK (route IN ('greek', 'foreign')),
    origin TEXT NOT NULL CHECK (origin = 'user_explicit'),
    routing_policy_version TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    note TEXT CHECK (note IS NULL OR (length(note) > 0 AND length(note) <= 280)),
    created_at TEXT NOT NULL
);
CREATE INDEX genre_routing_events_current_lookup_idx
    ON genre_routing_events(canonical_track_id, created_at DESC);
