import os
from llama_index.core.tools import FunctionTool
from core.db import get_db_conn, release_db_conn

# Allowlisted dangerous keywords — any query containing these is blocked
_SQL_BLOCKLIST = {"DROP", "DELETE", "TRUNCATE", "INSERT", "UPDATE", "ALTER", "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "COPY", "REPLACE", "MERGE"}

_sql_engine_singleton = None

def execute_hr_sql(input: str, user_role: str = "standard") -> str:
    """Executes a READ-ONLY PostgreSQL SELECT query on universal_rag_db.
    CRITICAL: Use this tool specifically when searching for enterprise_assets data (such as asset type, location, status).
    SCHEMA: The enterprise_assets table has columns: asset_id, asset_name, type, location, status, installation_date.
    Only SELECT queries are permitted — all write operations are rejected.
    """
    try:
        if not input or input == "None":
            return "SQL Error: No valid SQL query provided."

        # ── SQL injection / prompt-injection guard ──────────────────────────
        normalized = input.strip().upper()
        if not normalized.startswith("SELECT"):
            return "SQL Error: Only SELECT queries are permitted. Write operations are not allowed."
        
        # Block stacked statements explicitly
        if ";" in normalized:
            return "SQL Error: Stacked statements (using semicolons) are not allowed."

        # Block dangerous keywords in body
        for blocked in _SQL_BLOCKLIST:
            if f" {blocked} " in f" {normalized} ":
                return f"SQL Error: Keyword '{blocked}' is not allowed in safe query mode."

        conn = get_db_conn()
        try:
            cur = conn.cursor()
            # Inject RLS context so Postgres row-level policies apply
            cur.execute("SELECT set_config('app.current_user_role', %s, false);", (user_role,))
            cur.execute(input)
            res = cur.fetchall()
            cur.close()
        finally:
            release_db_conn(conn)
        return str(res)
    except Exception as e:
        return f"SQL Error: {str(e)}\nTry adjusting your SQL syntax."

def _build_sql_engine():
    global _sql_engine_singleton
    if _sql_engine_singleton is None:
        _sql_engine_singleton = FunctionTool.from_defaults(
            fn=execute_hr_sql, 
            name="structured_asset_database",
            description="Executes a PostgreSQL query on universal_rag_db and returns the results. Use this for all enterprise assets, facility, or well parameter queries."
        )
    return _sql_engine_singleton

def get_sql_engine():
    """Returns the cached SQL engine singleton."""
    return _build_sql_engine()
