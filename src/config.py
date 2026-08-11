"""Runtime configuration, read once from the environment.

Two connection identities exist and the distinction is the security model, not an
implementation detail:

* ``ADMIN_DSN``  — owns the schema. Used only by ``db/seed.py``.
* ``ANALYST_DSN`` — the read-only ``analyst_ro`` role. Used by everything that runs
  model-generated SQL. See docs/ADR/ADR-004-defence-in-depth-sql.md.
* ``STORE_DSN``   — the ``app_rw`` role on a SEPARATE database, ``ports_app``. Used only
  by ``src/store.py``. The agent's role holds no CONNECT there. See ADR-014.

Keeping them apart here, rather than passing a role name around, means there is no code
path in the agent that can accidentally acquire write access: the agent never sees the
admin or store credentials at all, and model-generated SQL only ever reaches the database
through the analyst connection in ``src/executor.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # local dev convenience; real deployments inject env vars directly


def _dsn(
    user_env: str,
    pw_env: str,
    default_user: str,
    default_pw: str,
    db: str | None = None,
) -> str:
    """One DSN. `db` overrides the analytics database, which the store needs (ADR-014).

    The store lives in its OWN database, not a schema of this one, so the override is the
    mechanism that keeps the agent's role out: `analyst_ro` holds CONNECT on the analytics
    database only, and PostgreSQL has no cross-database queries without FDW.
    """
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "55432")
    database = db if db is not None else os.getenv("POSTGRES_DB", "ports")
    user = os.getenv(user_env, default_user)
    pw = os.getenv(pw_env, default_pw)
    return f"host={host} port={port} dbname={database} user={user} password={pw}"


@dataclass(frozen=True)
class Settings:
    # --- database ---
    analyst_dsn: str
    admin_dsn: str

    # --- models (ADR-007) ---
    # Two tiers behind one wrapper. Provider is chosen by the model-string prefix, so
    # switching provider is an env change, not a code change.
    cheap_model: str
    strong_model: str

    # --- execution limits (ADR-004) ---
    statement_timeout_ms: int
    row_cap: int
    llm_timeout_s: int
    max_sql_retries: int
    max_question_chars: int

    # --- conversation (ADR-011) ---
    history_turns: int

    # --- runtime verification (ADR-012) ---
    runtime_verification: bool

    # --- the plain-language reading shown beside an answer (ADR-013) ---
    sql_reading: bool

    # --- conversation and telemetry store (ADR-014) ---
    store_dsn: str

    @staticmethod
    def load() -> Settings:
        return Settings(
            analyst_dsn=_dsn(
                "POSTGRES_ANALYST_USER", "POSTGRES_ANALYST_PASSWORD",
                "analyst_ro", "analyst_ro_pw",
            ),
            admin_dsn=_dsn(
                "POSTGRES_ADMIN_USER", "POSTGRES_ADMIN_PASSWORD",
                "postgres", "postgres",
            ),
            # Names match ADR-007. Defaults verified against LiteLLM's model registry on
            # 2026-08-04; both are current, and pre-2026 IDs such as claude-3-5-sonnet
            # have since been retired.
            cheap_model=os.getenv("MODEL_CHEAP", "anthropic/claude-haiku-4-5"),
            strong_model=os.getenv("MODEL_STRONG", "anthropic/claude-sonnet-5"),
            statement_timeout_ms=int(os.getenv("STATEMENT_TIMEOUT_MS", "5000")),
            row_cap=int(os.getenv("ROW_CAP", "500")),
            llm_timeout_s=int(os.getenv("LLM_TIMEOUT_S", "45")),
            max_sql_retries=int(os.getenv("MAX_SQL_RETRIES", "1")),
            # Bounds the untrusted text sent to the provider. The other limits here cap
            # what a query costs once the model has written it; this caps what the model
            # is asked to read, which nothing else does. A question this long is not a
            # question. It is either a paste of a document or an attempt to bury an
            # instruction where a reviewer will not see it, and both are cheaper to
            # refuse than to send. 2000 characters is several times the longest question
            # in the gold set, so it does not constrain real use.
            max_question_chars=int(os.getenv("MAX_QUESTION_CHARS", "2000")),
            # The rewrite window (ADR-011). Bounded for cost and for blast radius: a
            # longer window is more prior text for a rewrite to drag into a question the
            # user did not ask. Three exchanges covers the follow-up chains a demo
            # produces; it is not a memory feature.
            history_turns=int(os.getenv("HISTORY_TURNS", "3")),
            # ADR-012's three additions as one switch, so the eval can measure the
            # pipeline with and without them and attribute the difference. "off"
            # reproduces the pre-ADR-012 baseline (runs 15 to 19) exactly, which is what
            # makes the comparison a comparison rather than two unrelated numbers.
            # Defaults OFF, and the default is a measured decision rather than a
            # preference. Runs 20 to 25 alternated the feature on and off across the
            # 108-case set: groundedness rose (98.7% to 100% against 96.0% to 97.4%) and
            # execution accuracy fell by more (89.6% to 92.2% against 93.5% to 94.8%),
            # with every regression carrying a verifier_objection reason code. The
            # machinery, the switch and the evidence all stay; the default follows the
            # measurement. See ADR-012's addendum.
            runtime_verification=os.getenv("RUNTIME_VERIFICATION", "false").lower()
            in ("1", "true", "yes", "on"),
            # The verifier's READING, without the verifier's authority (ADR-013).
            #
            # Turning RUNTIME_VERIFICATION off took the "What was measured" line off the
            # screen with it, because the reading is produced by the same node as the
            # objection. Measured across runs 20 to 25: with verification on, all 75
            # answered cases carried a reading; with it off, none of 108 did. That left
            # the non-technical reader ADR-008 designs for with no verification surface
            # except the SQL, which is the artefact they came here to avoid.
            #
            # This switch runs `verify` for its reading and discards its objection. What
            # measured badly in ADR-012 was the REGENERATION an objection triggers, not
            # the sentence it writes, and the two known defects in VERIFY_SYSTEM are
            # defects of judgement rather than of description. So the SQL, the answer and
            # the chart are identical to running with everything off; the only costs are
            # one cheap-tier call and the latency it does not overlap.
            sql_reading=os.getenv("SQL_READING", "true").lower()
            in ("1", "true", "yes", "on"),
            # A THIRD identity, in a SEPARATE DATABASE, and both halves matter. `app_rw`
            # owns `ports_app` and has no business in the analytics database; `analyst_ro`
            # holds no CONNECT on `ports_app`, so the agent's role cannot open a session
            # there at all. Chat history is therefore unreachable by model-generated SQL
            # for the same structural reason writes are: a grant that was never made.
            #
            # A separate database rather than a schema because that is the shape of a real
            # deployment. The data an analyst agent queries is usually someone else's, with
            # its own owner, release cadence, workload and retention rules; application
            # state is not. Modelling it this way makes the step to a separate server a
            # connection string rather than a migration. See ADR-014.
            store_dsn=_dsn(
                "APP_STORE_USER",
                "APP_STORE_PASSWORD",
                "app_rw",
                "app_rw_pw",
                db=os.getenv("APP_STORE_DB", "ports_app"),
            ),
        )


settings = Settings.load()
