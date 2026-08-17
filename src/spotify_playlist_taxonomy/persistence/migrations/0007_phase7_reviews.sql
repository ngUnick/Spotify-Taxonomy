CREATE TABLE phase7_review_events (
    review_id TEXT PRIMARY KEY,
    canonical_track_id TEXT NOT NULL REFERENCES canonical_tracks(canonical_track_id),
    logical_playlist_id TEXT NOT NULL REFERENCES logical_playlists(logical_id),
    proposal_id TEXT REFERENCES phase6_proposals(proposal_id),
    action TEXT NOT NULL CHECK (action IN ('decide', 'clear')),
    decision TEXT CHECK (decision IN ('include', 'exclude', 'abstain')),
    origin TEXT NOT NULL CHECK (origin IN ('user_approval', 'user_override', 'user_explicit')),
    taxonomy_version TEXT NOT NULL,
    specification_version TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    note TEXT CHECK (note IS NULL OR length(note) <= 280),
    created_at TEXT NOT NULL
);
CREATE INDEX phase7_review_scope_idx ON phase7_review_events(canonical_track_id, logical_playlist_id, created_at);
