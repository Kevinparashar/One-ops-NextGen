-- _utils/backfill_missing_managers.sql  (uc08 approval — Step 3, org-chart completeness)
--
-- Completes the org chart so `manager_of_requester` resolves for every active
-- requester. Generic + derived — NO hardcoded user ids, account names, or tenant
-- ids. Each gap is filled by ROLE within the same tenant:
--   * active non-leadership user with no manager  -> a 'manager'-role user
--   * 'manager'-role user with no manager          -> an 'it_director'-role user
-- it_directors are top-of-chain and keep their NULL manager (legitimate).
--
-- SAFE: idempotent (only fills NULLs — never overwrites), same-tenant, points at
-- ACTIVE users only, guarded so it sets nothing when no candidate exists.
-- Re-runnable. Source of truth for seeded rows is data/itsm/sys_user.json.

BEGIN;

-- 1. non-leadership active users -> the tenant's manager-role user.
UPDATE itsm.sys_user AS u
   SET manager_id = (
         SELECT m.user_id FROM itsm.sys_user m
          WHERE m.tenant_id = u.tenant_id
            AND m.role = 'manager' AND m.is_active
          ORDER BY m.user_id LIMIT 1
       )
 WHERE u.manager_id IS NULL
   AND u.is_active
   AND u.role NOT IN ('manager', 'it_director')
   AND EXISTS (
         SELECT 1 FROM itsm.sys_user m
          WHERE m.tenant_id = u.tenant_id AND m.role = 'manager' AND m.is_active
       );

-- 2. manager-role users -> the tenant's it_director.
UPDATE itsm.sys_user AS u
   SET manager_id = (
         SELECT d.user_id FROM itsm.sys_user d
          WHERE d.tenant_id = u.tenant_id
            AND d.role = 'it_director' AND d.is_active
          ORDER BY d.user_id LIMIT 1
       )
 WHERE u.manager_id IS NULL
   AND u.is_active
   AND u.role = 'manager'
   AND EXISTS (
         SELECT 1 FROM itsm.sys_user d
          WHERE d.tenant_id = u.tenant_id AND d.role = 'it_director' AND d.is_active
       );

COMMIT;
