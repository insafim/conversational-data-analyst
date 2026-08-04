-- =============================================================================
-- The read-only analyst role — the load-bearing security control.
-- See docs/ADR/ADR-004-defence-in-depth-sql.md
--
-- The agent's database connection uses THIS role and never the owner. That is the
-- whole argument: a fully jailbroken model, generating any SQL it likes, still
-- cannot write, because the restriction is enforced by PostgreSQL's permission
-- system rather than by the model's willingness to comply.
--
-- Prompts can be fooled. Permissions cannot.
--
-- Runs after 01_schema.sql (filename order) so the tables exist to be granted on.
-- =============================================================================

-- Read the role password from the environment rather than hard-coding a credential
-- into tracked SQL. Set ANALYST_RO_PASSWORD in .env; docker-compose passes it through.
\set ro_password `echo "${ANALYST_RO_PASSWORD:-analyst_ro_pw}"`

-- ---------------------------------------------------------------------------
-- 1. Close the default door.
--
-- Historically every role got CREATE on the `public` schema via the PUBLIC
-- pseudo-role, which would let a "read-only" user create tables. PostgreSQL 15
-- changed this default, but we revoke explicitly so the guarantee does not depend
-- on which server version happens to be running.
-- ---------------------------------------------------------------------------
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- 2. The role itself. LOGIN, no CREATEDB, no CREATEROLE, no SUPERUSER, no BYPASSRLS.
-- ---------------------------------------------------------------------------
CREATE ROLE analyst_ro
    LOGIN
    PASSWORD :'ro_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 10;   -- bounds connection exhaustion from a runaway client

-- ---------------------------------------------------------------------------
-- 3. Grant exactly SELECT. Nothing else is granted anywhere.
--    No INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, CREATE, or USAGE
--    on sequences. Absence is the point: these are not revoked, they were never given.
-- ---------------------------------------------------------------------------
GRANT CONNECT ON DATABASE :"DBNAME" TO analyst_ro;
GRANT USAGE   ON SCHEMA public      TO analyst_ro;
GRANT SELECT  ON ALL TABLES IN SCHEMA public TO analyst_ro;

-- Future tables created by this owner are also readable, so adding a table does not
-- silently break the agent and does not tempt anyone into granting something wider.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analyst_ro;

-- ---------------------------------------------------------------------------
-- 4. Resource limits, attached to the role so they apply to every session it opens
--    and do not depend on the application remembering to set them.
--
--    HONEST LIMITATION: all three of these are USERSET parameters, which means the
--    session can raise or disable them with a SET statement. They are therefore
--    protection against runaway queries, NOT against a determined adversary. The
--    GRANTs above are the boundary that cannot be talked around; these are seatbelts.
--    src/executor.py sets the statement timeout again per connection for the same
--    defence-in-depth reason.
-- ---------------------------------------------------------------------------
ALTER ROLE analyst_ro SET statement_timeout = '5s';
ALTER ROLE analyst_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE analyst_ro SET default_transaction_read_only = on;

-- ---------------------------------------------------------------------------
-- 5. Remove the denial-of-service primitives that PostgreSQL grants to PUBLIC.
--
--    A SELECT-only role cannot damage data, but `SELECT pg_sleep(3600)` would hold a
--    connection open indefinitely. statement_timeout already bounds this; revoking
--    execution removes the primitive itself, which is the more durable fix.
--
--    Note what is NOT needed here: pg_read_file(), pg_ls_dir() and lo_import() are
--    restricted to superusers and the pg_read_server_files role by default, and
--    analyst_ro is a member of neither, so a file-read escape is already unreachable.
-- ---------------------------------------------------------------------------
REVOKE EXECUTE ON FUNCTION pg_sleep(double precision) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_sleep_for(interval)     FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_sleep_until(timestamp with time zone) FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- 6. What this role can still do, stated plainly so the residual risk is documented
--    rather than discovered:
--
--    * Read every row of every table in `public`. There is no row-level security and
--      no column masking — the demo data is synthetic, so this is acceptable here.
--      Real client data would need RLS policies per tenant; see the README's
--      path-to-production section.
--    * Read system catalogs (pg_catalog, information_schema), i.e. learn the schema.
--      This is required — src/schema.py depends on it — and exposes structure, not
--      customer data. pg_authid, which holds password hashes, remains superuser-only.
--    * Consume CPU with an expensive query (large cross join). Bounded by
--      statement_timeout and by the row cap applied in src/executor.py.
-- ---------------------------------------------------------------------------
