-- Stratum-Memory: non-privileged application role.
-- The owner/superuser role is provided by POSTGRES_USER (default: stratum).
-- The app role NEVER owns tables and has no BYPASSRLS, so RLS applies to it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stratum_app') THEN
        CREATE ROLE stratum_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO stratum_app;
