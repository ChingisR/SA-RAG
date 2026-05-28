from fastapi import APIRouter, Depends, HTTPException
from core.security import get_current_user

router = APIRouter(prefix="/graphrag", tags=["GraphRAG"])

@router.post("/build-summaries")
async def trigger_graphrag_summaries(current_user: dict = Depends(get_current_user)):
    """Triggers the Celery batch job to construct Executive Summaries from the Neo4j Graph."""
    user_role = current_user.get("role", "standard")
    if user_role not in ("Operations_Admin", "admin"):
        raise HTTPException(status_code=403, detail="GraphRAG triggers restricted to Operations_Admin.")
    
    from worker import build_graphrag_summaries
    task = build_graphrag_summaries.delay()
    return {
        "status": "dispatched",
        "task_id": task.id,
        "message": f"Summaries construction job successfully dispatched (Task ID: {task.id})."
    }

