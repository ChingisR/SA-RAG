import os
import requests
from fastapi import APIRouter, HTTPException
import structlog
from core.db import get_db_conn, release_db_conn

logger = structlog.get_logger()
router = APIRouter(prefix="/health", tags=["Health"])

@router.get("")
@router.get("/live")
def health_live():
    """Lightweight liveness probe — Docker & Portainer healthcheck target."""
    return {"status": "ok", "service": "fastapi-agentic-rag"}

@router.get("/ready")
def health_ready():
    """Deep readiness probe: Checks Postgres and Core Services."""
    # Note: gpu_breaker should be imported or managed if we fully decouple
    # For now, we will do a basic DB check and Opensearch check
    status_dict = {"status": "ok", "checks": {}}
    
    # 1. Database connection check
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        release_db_conn(conn)
        status_dict["checks"]["postgres"] = "up"
    except Exception as e:
        status_dict["status"] = "error"
        status_dict["checks"]["postgres"] = f"down: {str(e)}"
        logger.error("readiness_probe_db_fail", error=str(e))
    
    # 2. OpenSearch cluster check
    os_url  = os.getenv("OPENSEARCH_URL", "https://opensearch:9200")
    os_user = os.getenv("OPENSEARCH_USER")
    os_pass = os.getenv("OPENSEARCH_PASSWORD")
    if os_user and os_pass:
        try:
            res = requests.get(f"{os_url}/_cluster/health", auth=(os_user, os_pass), verify=False, timeout=3)
            res.raise_for_status()
            os_status = res.json().get("status", "unknown")
            status_dict["checks"]["opensearch"] = os_status
            if os_status == "red":
                status_dict["status"] = "error"
                logger.error("readiness_probe_os_red_status")
        except Exception as e:
            status_dict["status"] = "error"
            status_dict["checks"]["opensearch"] = f"down: {str(e)}"
            logger.error("readiness_probe_os_fail", error=str(e))

    # 3. Arize Phoenix telemetry check
    try:
        phoenix_url = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://phoenix:4318/v1/traces")
        # Derive the Phoenix web UI origin (port 6006) from the OTLP endpoint
        phoenix_host = phoenix_url.split(":")[1].lstrip("/")  # e.g. "phoenix"
        phoenix_health = requests.get(f"http://{phoenix_host}:6006", timeout=2, allow_redirects=False)
        status_dict["checks"]["phoenix"] = "up" if phoenix_health.status_code < 500 else "degraded"
    except Exception:
        # Phoenix is non-critical — log warning but do NOT degrade the overall status
        status_dict["checks"]["phoenix"] = "unreachable"
        logger.warning("readiness_probe_phoenix_unreachable")
        
    if status_dict["status"] != "ok":
        raise HTTPException(status_code=503, detail=status_dict)
    
    logger.info("readiness_probe_success", checks=status_dict["checks"])
    return status_dict
