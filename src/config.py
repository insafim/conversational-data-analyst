"""Runtime configuration, read once from the environment.

Two connection identities exist and the distinction is the security model, not an
implementation detail:

* ``ADMIN_DSN``  — owns the schema. Used only by ``db/seed.py``.
* ``ANALYST_DSN`` — the read-only ``analyst_ro`` role. Used by everything that runs
  model-generated SQL. See docs/ADR/ADR-004-defence-in-depth-sql.md.

Keeping them apart here, rather than passing a role name around, means there is no code
path in the agent that can accidentally acquire write access: the agent never sees the
admin credentials at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # local dev convenience; real deployments inject env vars directly


def _dsn(user_env: str, pw_env: str, default_user: str, default_pw: str) -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "55432")
    db = os.getenv("POSTGRES_DB", "ports")
    user = os.getenv(user_env, default_user)
    pw = os.getenv(pw_env, default_pw)
    return f"host={host} port={port} dbname={db} user={user} password={pw}"


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
        )


settings = Settings.load()
