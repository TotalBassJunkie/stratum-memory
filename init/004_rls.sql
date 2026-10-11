-- Stratum-Memory: Row-Level Security (WP-12 foundation).
-- Tables are owned by the init user (superuser 'stratum'). The app role 'stratum_app'
-- is a non-owner => RLS applies to it. FORCE RLS is hardening for a future non-superuser owner.

DO $$
DECLARE
    t text;
    -- NOTE: uri_registry is handled separately below (its column is `tenant`,
    -- not `tenant_id`). It must NOT be in this list, or the whole DO block
    -- aborts and rolls back every policy and grant.
    tbls text[] := ARRAY[
      'entities','facts','edges','files','chunks','kv','staging_candidates',
      'memory_events','syntheses','api_keys','audit_log','jobs'
    ];
BEGIN
    FOREACH t IN ARRAY tbls LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', t);
        EXECUTE format($f$
            DROP POLICY IF EXISTS tenant_isolation ON %I;
            CREATE POLICY tenant_isolation ON %I
              USING (
                tenant_id = current_setting('stratum.tenant', true)
                OR COALESCE(current_setting('stratum.bypass_rls', true), 'off') = 'on'
              )
              WITH CHECK (
                tenant_id = current_setting('stratum.tenant', true)
                OR COALESCE(current_setting('stratum.bypass_rls', true), 'off') = 'on'
              );
        $f$, t, t);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO stratum_app;', t);
    END LOOP;
END
$$;

-- audit_log has no tenant_id NOT NULL for system rows; add a permissive fallback already covered
-- by the bypass GUC. Grant sequence usage for audit_log audit_id.
GRANT USAGE, SELECT ON SEQUENCE audit_log_audit_id_seq TO stratum_app;

-- tenants is deliberately NOT under the tenant RLS policy: tenant_id is its own
-- primary key and rows are global, so a per-tenant USING clause makes no sense
-- (there would be no row visible to set the GUC against in the first place).
-- The app role still needs table privileges to register a tenant during an
-- import (the RLS-bypass GUC bypasses the *policy*, never the GRANT), so grant
-- exactly the two verbs that are legitimate: read + create. No UPDATE/DELETE.
GRANT SELECT, INSERT ON tenants TO stratum_app;

-- memory_events is an append-only truth journal: the app role may only read and insert,
-- never update or delete (schema-level enforcement of build-kit fact #4 / MASTER-PLAN §5).
REVOKE UPDATE, DELETE ON memory_events FROM stratum_app;
GRANT SELECT, INSERT ON memory_events TO stratum_app;

-- api_keys and uri_registry have tenant_id? api_keys yes; uri_registry uses 'tenant' column.
-- Re-point uri_registry policy to its real column name.
DROP POLICY IF EXISTS tenant_isolation ON uri_registry;
ALTER TABLE uri_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE uri_registry FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON uri_registry
  USING (
    tenant = current_setting('stratum.tenant', true)
    OR COALESCE(current_setting('stratum.bypass_rls', true), 'off') = 'on'
  )
  WITH CHECK (
    tenant = current_setting('stratum.tenant', true)
    OR COALESCE(current_setting('stratum.bypass_rls', true), 'off') = 'on'
  );
-- uri_registry is excluded from the DO-loop above, so grant it explicitly here.
GRANT SELECT, INSERT, UPDATE, DELETE ON uri_registry TO stratum_app;
