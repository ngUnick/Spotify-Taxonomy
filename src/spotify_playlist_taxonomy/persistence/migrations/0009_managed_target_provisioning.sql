CREATE TABLE managed_target_registrations (
    output_target_id TEXT PRIMARY KEY,
    registration_id TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK (provider = 'spotify'),
    account_binding_id TEXT NOT NULL REFERENCES platform_account_bindings(binding_id),
    provider_playlist_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('provisioning', 'active', 'recovery_required')),
    provenance TEXT NOT NULL CHECK (provenance = 'phase10_create'),
    created_at TEXT NOT NULL,
    registered_at TEXT,
    CHECK (
        (state = 'active' AND provider_playlist_id IS NOT NULL AND registered_at IS NOT NULL)
        OR state IN ('provisioning', 'recovery_required')
    )
);
