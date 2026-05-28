from fastapi import APIRouter, Depends, HTTPException
from core.security import get_current_user
from core.db import get_db_conn, release_db_conn

router = APIRouter(prefix="/analytics", tags=["Analytics"])

@router.get("")
async def get_analytics(current_user: dict = Depends(get_current_user)):
    """Enhanced analytics: department metrics, cache stats, query volume, top queries."""
    user_role = current_user.get("role", "standard")
    if user_role not in ("Operations_Admin", "admin"):
        raise HTTPException(status_code=403, detail="Analytics access restricted to Operations_Admin.")

    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()

            # Dept salary metrics (RLS-aware)
            cur.execute("SELECT set_config('app.current_user_role', 'HR_Admin', false);")
            cur.execute("SELECT department, AVG(salary), COUNT(*) FROM hr_employees GROUP BY department ORDER BY department;")
            dept_data = [{"department": row[0], "average_salary": float(row[1] or 0), "employee_count": row[2]} for row in cur.fetchall()]

            # Cache stats
            cur.execute("SELECT COUNT(*) FROM semantic_cache;")
            total_cache = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM semantic_cache WHERE created_at >= NOW() - INTERVAL '24 hours';")
            cache_24h = cur.fetchone()[0]

            # Query volume: last 7 days
            cur.execute("""
                SELECT DATE(created_at) AS day, COUNT(*) AS queries
                FROM chat_messages WHERE role = 'user'
                  AND created_at >= NOW() - INTERVAL '7 days'
                GROUP BY day ORDER BY day;
            """)
            query_volume = [{"date": str(row[0]), "queries": row[1]} for row in cur.fetchall()]

            # Top 10 most-queried topics from cache
            cur.execute("""
                SELECT query_text, created_at FROM semantic_cache
                ORDER BY created_at DESC LIMIT 10;
            """)
            recent_queries = [{"query": row[0][:120], "at": str(row[1])} for row in cur.fetchall()]

            # Session stats
            cur.execute("SELECT COUNT(*) FROM chat_sessions;")
            total_sessions = cur.fetchone()[0]

            cur.execute("SELECT COUNT(DISTINCT user_email) FROM chat_sessions;")
            unique_users = cur.fetchone()[0]

            cur.close()
        finally:
            release_db_conn(conn)

        return {
            "department_metrics": dept_data,
            "total_cache_entries": total_cache,
            "cache_entries_last_24h": cache_24h,
            "query_volume_7d": query_volume,
            "recent_queries": recent_queries,
            "total_sessions": total_sessions,
            "unique_users": unique_users,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
