"""
Database configuration and session management module.

This module initializes the PostgreSQL connection pool and manages the schema
for the relational database (Postgres + pgvector). It defines the tables for:
- Semantic Caching (for fast ANN lookup using HNSW)
- Users and Authentication
- Chat Sessions and Messages
- Feedback mechanisms
- Enterprise Assets (with Row-Level Security)
"""

import os
import psycopg2
from psycopg2 import pool as _pg_pool

from core.config import PG_DSN, CACHE_DIM, DIMENSIONS
from core.security import hash_password

# Global threaded connection pool to handle concurrent FastAPI requests efficiently.
_pg_connection_pool: _pg_pool.ThreadedConnectionPool | None = None

def init_db_pool():
    """
    Initialize the global database connection pool.
    
    This ensures that concurrent API requests can borrow existing database connections
    without the high overhead of establishing new TCP/IP connections per request.
    Configured for a minimum of 2 and maximum of 20 concurrent connections.
    """
    global _pg_connection_pool
    if _pg_connection_pool is None or _pg_connection_pool.closed:
        _pg_connection_pool = _pg_pool.ThreadedConnectionPool(minconn=2, maxconn=20, dsn=PG_DSN)
        print("✅ Postgres connection pool initialised (2–20 connections).")

def close_db_pool():
    """
    Close the global database connection pool gracefully during application shutdown.
    """
    global _pg_connection_pool
    if _pg_connection_pool and not _pg_connection_pool.closed:
        _pg_connection_pool.closeall()
        print("✅ Postgres connection pool closed.")

def get_db_conn():
    """
    Borrow a database connection from the global pool.
    
    Returns:
        psycopg2.extensions.connection: A thread-safe database connection.
        
    Note:
        Caller MUST call release_db_conn(conn) when finished to avoid starving the pool.
    """
    global _pg_connection_pool
    if _pg_connection_pool is None or _pg_connection_pool.closed:
        init_db_pool()
    return _pg_connection_pool.getconn()

def release_db_conn(conn):
    """
    Return a borrowed connection back to the global pool.
    """
    if _pg_connection_pool and not _pg_connection_pool.closed:
        _pg_connection_pool.putconn(conn)

def init_db():
    """
    Initialize the PostgreSQL database schema.
    
    This function:
    1. Enables the pgvector extension for similarity search.
    2. Creates the semantic_cache table and its HNSW index.
    3. Handles lightweight migrations (e.g. altering columns for older deployments).
    4. Creates standard relational tables (users, chat_sessions, chat_messages, feedback).
    5. Seeds the default admin user safely.
    6. Creates enterprise_assets table and enables Row-Level Security (RLS) policies.
    """
    try:
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        
        # 1. Enable vector extension for embeddings
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        # --- Semantic Cache Table ---
        # Use DIMENSIONS/2 sliced Matryoshka vectors for PG HNSW lookup performance.
        # NOTE: We intentionally do NOT drop this table on restart — cached
        # query-response pairs are valuable and must survive container restarts.
        cache_dim = CACHE_DIM
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id SERIAL PRIMARY KEY,
                query_text TEXT,
                response_text TEXT,
                embedding vector({cache_dim}),
                allowed_roles TEXT[],
                source_document_hashes TEXT[],
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        # Rename allowed_groups to allowed_roles if it exists from older deployments (Migration step)
        try:
            cur.execute("ALTER TABLE semantic_cache RENAME COLUMN allowed_groups TO allowed_roles;")
            cur.execute("ALTER INDEX IF EXISTS idx_cache_allowed_groups RENAME TO idx_cache_allowed_roles;")
        except Exception:
            pass
            
        # Ensure allowed_roles and source_document_hashes columns exist for older deployments
        try:
            cur.execute("ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS allowed_roles TEXT[];")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cache_allowed_roles ON semantic_cache USING GIN (allowed_roles);")
            cur.execute("ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS source_document_hashes TEXT[];")
        except Exception as e:
            print(f"Notice: Failed to alter semantic_cache (might already exist or differ): {e}")
            
        # Add HNSW (Hierarchical Navigable Small World) index for extremely fast ANN (Approximate Nearest Neighbor) lookups at scale.
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS semantic_cache_embedding_hnsw
            ON semantic_cache USING hnsw (embedding vector_cosine_ops);
        """)
        
        # TTL cleanup: Delete semantic cache entries older than 7 days on every startup to prevent stale context.
        cur.execute("DELETE FROM semantic_cache WHERE created_at < NOW() - INTERVAL '7 days';")

        # --- Users Table ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            preferred_agent TEXT DEFAULT 'langgraph',
            output_thinking BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)
        # For existing databases — add columns if they don't already exist (Migration step)
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_agent TEXT DEFAULT 'langgraph';")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS output_thinking BOOLEAN DEFAULT FALSE;")
        except Exception:
            pass

        # --- Chat Sessions Table ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id VARCHAR(50) PRIMARY KEY,
            user_email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
            title TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        # --- Chat Messages Table ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            session_id VARCHAR(50) NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL,
            sources_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        # --- Message Feedback Table ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS message_feedback (
            id SERIAL PRIMARY KEY,
            message_id TEXT NOT NULL,
            user_email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK (rating IN (1, -1)),
            comment TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (message_id, user_email)
        );
        """)

        # Seed default admin ONLY if no users exist — never overwrite existing passwords.
        admin_email = os.getenv("ADMIN_EMAIL", "admin@enterprise.com")
        admin_password = os.getenv("ADMIN_DEFAULT_PASSWORD")
        if not admin_password:
            raise RuntimeError("FATAL: ADMIN_DEFAULT_PASSWORD must be explicitly set in the environment.")
        cur.execute("SELECT COUNT(*) FROM users WHERE email = %s", (admin_email,))
        if cur.fetchone()[0] == 0:
            default_pw_hash = hash_password(admin_password)
            cur.execute("""
                INSERT INTO users (email, password_hash, role)
                VALUES (%s, %s, 'admin')
                ON CONFLICT (email) DO NOTHING;
            """, (admin_email, default_pw_hash))
            print(f"✅ Default admin user '{admin_email}' created.")
        else:
            print(f"ℹ️ Admin user '{admin_email}' already exists, skipping seed.")

        # --- Enterprise Assets Table ---
        cur.execute("CREATE TABLE IF NOT EXISTS enterprise_assets (asset_id SERIAL PRIMARY KEY, asset_name VARCHAR(100), type VARCHAR(50), location VARCHAR(100), status VARCHAR(50), installation_date DATE);")

        # --- Enforce Row-Level Security (RLS) ---
        # RLS restricts which users can view records based on the app.current_user_role setting.
        cur.execute("""
            ALTER TABLE enterprise_assets ENABLE ROW LEVEL SECURITY;
            ALTER TABLE enterprise_assets FORCE ROW LEVEL SECURITY;
            
            -- Grant full SELECT access to Operations_Admin users
            DROP POLICY IF EXISTS "ops_admin_all" ON enterprise_assets;
            CREATE POLICY "ops_admin_all" ON enterprise_assets FOR SELECT TO public USING (current_setting('app.current_user_role', true) = 'Operations_Admin');
            
            -- Deny access to all standard users
            DROP POLICY IF EXISTS "standard_user_none" ON enterprise_assets;
            CREATE POLICY "standard_user_none" ON enterprise_assets FOR SELECT TO public USING (current_setting('app.current_user_role', true) != 'Operations_Admin' AND 1=0);
        """)

        # Seed mock asset data if table is empty
        cur.execute("SELECT COUNT(*) FROM enterprise_assets;")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO enterprise_assets (asset_name, type, location, status, installation_date) VALUES
                ('Kashagan Offshore Platform', 'Platform', 'Caspian Sea Block 1', 'Active', '2016-09-01'),
                ('Atyrau Processing Plant', 'Processing Plant', 'Atyrau Region', 'Active', '2016-10-15'),
                ('H2S Scrubbing Unit B', 'Equipment', 'Bolashak Onshore Processing Facility', 'Maintenance', '2020-03-22'),
                ('Drilling Rig Caspian-D', 'Rig', 'North Caspian Sea', 'Standby', '2022-11-05');
            """)
            
        cur.close()
        conn.close()
    except Exception as e:
        print(f"🚨 DB Init Error: {e}")

