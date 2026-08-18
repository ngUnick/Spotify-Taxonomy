ALTER TABLE managed_target_policy_repair_attempts
ADD COLUMN http_status INTEGER CHECK (http_status BETWEEN 100 AND 599);

ALTER TABLE managed_target_policy_repair_attempts
ADD COLUMN request_sent INTEGER CHECK (request_sent IN (0, 1));

ALTER TABLE managed_target_policy_repair_attempts
ADD COLUMN provider_acknowledged INTEGER CHECK (provider_acknowledged IN (0, 1));

ALTER TABLE managed_target_policy_repair_attempts
ADD COLUMN readback_public TEXT CHECK (readback_public IN ('true', 'false', 'null'));

ALTER TABLE managed_target_policy_repair_attempts
ADD COLUMN readback_collaborative TEXT CHECK (readback_collaborative IN ('true', 'false'));
