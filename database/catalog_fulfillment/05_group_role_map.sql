-- catalog_fulfillment/05_group_role_map.sql  (uc08 approval — owning-group resolution)
--
-- Table-backed bridge: owner_group -> the sys_user attribute (role|department)
-- that staffs it. Config-as-code, consistent with itsm.approval_policy:
--   data/itsm/group_role_map.json  --load_group_role_map.py-->  this table.
-- The runtime resolver JOINs this table with itsm.sys_user in a SINGLE query
-- (no extra round-trip vs. the previous in-memory lookup — zero added latency).
-- The Phase-2 HR/IdP sync populates this table directly; the resolver is unchanged.
-- Additive + idempotent. Runs after 04.

BEGIN;

CREATE TABLE IF NOT EXISTS itsm.group_role_map (
  owner_group  TEXT NOT NULL,
  -- which sys_user column staffs this group
  attribute    TEXT NOT NULL CHECK (attribute IN ('role', 'department')),
  value        TEXT NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_group)
);

COMMIT;
