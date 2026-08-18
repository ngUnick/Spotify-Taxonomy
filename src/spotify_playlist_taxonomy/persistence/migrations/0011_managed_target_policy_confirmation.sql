ALTER TABLE managed_target_registrations
ADD COLUMN policy_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (policy_confirmed IN (0, 1));
