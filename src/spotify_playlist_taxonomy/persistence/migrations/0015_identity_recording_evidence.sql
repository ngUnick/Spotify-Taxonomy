ALTER TABLE track_identities ADD COLUMN duration_ms INTEGER
    CHECK (duration_ms IS NULL OR duration_ms >= 0);
ALTER TABLE track_identities ADD COLUMN isrc TEXT;
