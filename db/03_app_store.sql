-- =============================================================================
-- The application's own database: saved conversations and per-turn telemetry.
-- See docs/ADR/ADR-014-conversation-store.md
--
-- A SEPARATE DATABASE, not a table in `ports` and not a schema inside it. The
-- reason is the shape of a real deployment rather than anything local: the data
-- an analyst agent queries is almost always someone else's (a warehouse, a read
-- replica, a customer's database), while chat history is the application's own
-- state. They differ in ownership, in release cadence, in workload, and in the
-- retention rules that apply to them, so they are separate systems in
-- production. Modelling that here means the step to a separate server later is a
-- connection string, not a migration.
--
-- It is also the strongest isolation available in one PostgreSQL instance.
-- `analyst_ro` is granted no CONNECT here, so the agent's role cannot open a
-- session at all, and PostgreSQL has no cross-database queries without FDW.
-- Compare the alternative of keeping history in `ports`: 02_roles.sql grants
-- analyst_ro SELECT on every table in `public` and, via ALTER DEFAULT
-- PRIVILEGES, on every table added later, so history there would be readable by
-- the agent. That matters twice over: "what have other people asked?" becomes a
-- question it answers, and the database it queries starts holding user-supplied
-- text, which is the second-order injection channel src/prompts.py documents and
-- tests/test_second_order_injection.py polices.
--
-- Verified rather than assumed: tests/test_store_isolation.py tries to connect
-- as the agent's role and asserts the failure.
--
-- Runs after 02_roles.sql (filename order) so analyst_ro exists to be denied.
-- =============================================================================

\set app_password `echo "${APP_STORE_PASSWORD:-app_rw_pw}"`
\set app_db `echo "${APP_STORE_DB:-ports_app}"`

-- ---------------------------------------------------------------------------
-- 1. The writer role. Narrow on purpose: it owns its own database and has no
--    business with `ports` at all. Not a superuser, so a compromise of the
--    application process cannot reach the analytics data with anything more than
--    the SELECT that analyst_ro already has.
-- ---------------------------------------------------------------------------
CREATE ROLE app_rw
    LOGIN
    PASSWORD :'app_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 10;

-- ---------------------------------------------------------------------------
-- 2. The database, owned by app_rw so the application can create its own tables
--    without holding rights anywhere else.
-- ---------------------------------------------------------------------------
CREATE DATABASE :"app_db" OWNER app_rw;

COMMENT ON DATABASE :"app_db" IS
    'Application state: saved conversations and per-turn telemetry. Separate from the '
    'analytics database by design; analyst_ro holds no CONNECT here.';

-- ---------------------------------------------------------------------------
-- 3. Close the default door, and this is the line the whole file turns on.
--
--    PostgreSQL grants CONNECT on a new database to the PUBLIC pseudo-role, and
--    every login role inherits PUBLIC. Without this REVOKE, analyst_ro could
--    open a session here and read whatever it found, and the separation above
--    would be decorative. Creating a separate database is not, by itself, an
--    access control.
-- ---------------------------------------------------------------------------
REVOKE ALL ON DATABASE :"app_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"app_db" TO app_rw;

-- ---------------------------------------------------------------------------
-- 4. The tables, created inside the new database.
--
--    `\connect` switches this psql session; everything below runs against the
--    application database rather than `ports`.
-- ---------------------------------------------------------------------------
\connect :"app_db"

-- `public` in a fresh database also carries PUBLIC privileges. The owner is the
-- only role that needs them here.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO app_rw;

SET ROLE app_rw;

CREATE TABLE IF NOT EXISTS conversation (
    id          text PRIMARY KEY,
    title       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE conversation IS 'One saved chat. Ordered in the sidebar by updated_at.';

CREATE TABLE IF NOT EXISTS turn (
    id              text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    seq             integer NOT NULL,
    asked_at        timestamptz NOT NULL DEFAULT now(),
    question        text NOT NULL,
    -- The whole AgentResult, rows included, so a reopened conversation renders
    -- identically. jsonb rather than text because the observability page
    -- aggregates over these fields, and aggregating in SQL beats loading every
    -- turn into Python to average one number.
    result          jsonb NOT NULL,
    UNIQUE (conversation_id, seq)
);

COMMENT ON TABLE turn IS
    'One exchange. `result` is the serialised AgentResult, which is also what the '
    'observability page reads, so the two views cannot disagree about a turn.';

-- The panel scans turns by recency across conversations; the sidebar scans
-- conversations by recency. Both are ordered scans that would otherwise walk the
-- table once the store holds a few thousand rows.
CREATE INDEX IF NOT EXISTS turn_by_time ON turn (asked_at DESC);
CREATE INDEX IF NOT EXISTS conversation_by_update ON conversation (updated_at DESC);

RESET ROLE;

-- ---------------------------------------------------------------------------
-- 5. Residual risk, stated rather than discovered.
--
--    analyst_ro cannot connect here, so it cannot read these tables or learn
--    their contents. It CAN still see that the database exists, because
--    pg_database is world-readable and src/schema.py depends on catalog access
--    generally. A database name is not a conversation, and src/validator.py
--    denies catalog queries arriving from user questions in any case.
--
--    There is no row-level security on these tables, because there is one
--    implicit user. Multi-tenant deployment needs identity first, then RLS keyed
--    to it, which is the same precondition ADR-004 names for the business data.
-- ---------------------------------------------------------------------------
