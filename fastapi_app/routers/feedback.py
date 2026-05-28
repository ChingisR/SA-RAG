from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.db import get_db_conn, release_db_conn
from core.security import get_current_user

router = APIRouter(tags=["feedback"])

class FeedbackRequest(BaseModel):
    message_id: str
    rating: int          # 1 = thumbs up, -1 = thumbs down
    comment: Optional[str] = None

@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest, current_user: dict = Depends(get_current_user)):
    """Record user feedback on an AI response for quality auditing."""
    if req.rating not in (1, -1):
        raise HTTPException(status_code=422, detail="rating must be 1 (positive) or -1 (negative).")
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO message_feedback (message_id, user_email, rating, comment)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (message_id, user_email) DO UPDATE SET rating = EXCLUDED.rating, comment = EXCLUDED.comment;
            """, (req.message_id, current_user["email"], req.rating, req.comment))
            conn.commit()
            cur.close()
        finally:
            release_db_conn(conn)
        return {"status": "recorded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
