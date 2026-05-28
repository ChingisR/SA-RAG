import uuid
from typing import List
from datetime import datetime
import json
from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel
import psycopg2

from core.db import get_db_conn, release_db_conn
from core.security import get_current_user

router = APIRouter(tags=["sessions"])

class ChatSessionResponse(BaseModel):
    session_id: str
    title: str
    created_at: str

@router.post("/sessions", response_model=ChatSessionResponse)
async def create_session(title: str = Query("New Chat"), current_user: dict = Depends(get_current_user)):
    session_id = str(uuid.uuid4())
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO chat_sessions (session_id, user_email, title) VALUES (%s, %s, %s)",
                (session_id, current_user["email"], title)
            )
            conn.commit()
            cur.close()
        finally:
            release_db_conn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"session_id": session_id, "title": title, "created_at": datetime.utcnow().isoformat()}

@router.get("/sessions", response_model=List[ChatSessionResponse])
async def list_sessions(current_user: dict = Depends(get_current_user)):
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT session_id, title, created_at FROM chat_sessions WHERE user_email = %s ORDER BY created_at DESC",
                (current_user["email"],)
            )
            sessions = [{"session_id": row[0], "title": row[1], "created_at": row[2].isoformat() if row[2] else ""} for row in cur.fetchall()]
            cur.close()
            return sessions
        finally:
            release_db_conn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, current_user: dict = Depends(get_current_user)):
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_email FROM chat_sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
            if not row or row[0] != current_user["email"]:
                cur.close()
                raise HTTPException(status_code=404, detail="Session not found")
                
            cur.execute(
                "SELECT id, role, content, sources_json FROM chat_messages WHERE session_id = %s ORDER BY created_at ASC",
                (session_id,)
            )
            messages = []
            for msg in cur.fetchall():
                msg_dict = {"id": str(msg[0]), "role": msg[1], "content": msg[2]}
                if msg[3]:
                    try:
                        msg_dict["sources"] = json.loads(msg[3])
                    except:
                        pass
                messages.append(msg_dict)
            cur.close()
            return messages
        finally:
            release_db_conn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, current_user: dict = Depends(get_current_user)):
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM chat_sessions WHERE session_id = %s AND user_email = %s", (session_id, current_user["email"]))
            conn.commit()
            cur.close()
            return {"status": "success"}
        finally:
            release_db_conn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
