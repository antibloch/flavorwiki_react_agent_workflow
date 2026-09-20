"""Validate deployment configuration and live PostgreSQL access without making an LLM call."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/preflight.py` from the deployment directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.engine import make_url

import funda_agent_exp as agent


def main() -> int:
    url = make_url(agent.DATABASE_URI)
    print(
        "database target:",
        f"{url.host}:{url.port or 5432}/{url.database}",
        f"(connect timeout {agent.DB_CONNECT_TIMEOUT_S}s)",
    )
    print("model:", agent.MODEL_NAME)
    print("reasoning effort:", agent.MODEL_REASONING_EFFORT or "default")

    try:
        with agent._sql_engine().connect() as conn:
            row = conn.execute(
                text(
                    "SELECT current_database() AS database_name, "
                    "current_user AS database_role, "
                    "to_regclass('public.survey') IS NOT NULL AS survey_table_available"
                )
            ).mappings().one()
    except Exception as exc:  # noqa: BLE001 - command-line preflight reports a clean failure
        print(f"live database: FAILED ({type(exc).__name__})", file=sys.stderr)
        return 1

    print(
        "live database: OK",
        f"database={row['database_name']}",
        f"role={row['database_role']}",
        f"survey_table={row['survey_table_available']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
