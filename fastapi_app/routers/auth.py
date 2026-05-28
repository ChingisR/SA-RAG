from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import jwt
from datetime import datetime, timedelta, timezone
import structlog
import psycopg2

from core.db import get_db_conn, release_db_conn
from core.security import (
    verify_password,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SECRET_KEY,
    ALGORITHM,
    get_current_user
)

logger = structlog.get_logger()

router = APIRouter(tags=["auth"])

class LoginRequest(BaseModel):
    email: str
    password: str

@router.post("/login")
async def login(req: LoginRequest):
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT password_hash, role FROM users WHERE email = %s", (req.email,))
            row = cur.fetchone()
            cur.close()
        finally:
            release_db_conn(conn)
        
        if not row:
            logger.warning("login_user_not_found", email=req.email)
            raise HTTPException(status_code=401, detail="Incorrect email or password")
            
        stored_hash = row[0].strip()
        if verify_password(req.password, stored_hash):
            logger.info("login_success", email=req.email)
            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            expire = datetime.now(timezone.utc) + access_token_expires
            to_encode = {"sub": req.email, "role": row[1], "exp": expire}
            encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
            return {"access_token": encoded_jwt, "token_type": "bearer", "user": {"email": req.email, "role": row[1]}}
        else:
            logger.info("login_password_mismatch", email=req.email)
            raise HTTPException(status_code=401, detail="Incorrect email or password")
    except psycopg2.Error as db_e:
        logger.error("login_db_error", error=str(db_e))
        raise HTTPException(status_code=500, detail="Internal Database Connection Error")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("login_general_error", error=str(e))
        raise HTTPException(status_code=401, detail="Incorrect email or password")

class SettingsUpdateRequest(BaseModel):
    preferred_agent: str
    output_thinking: bool = False

@router.get("/settings")
async def get_user_settings(current_user: dict = Depends(get_current_user)):
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT preferred_agent, output_thinking FROM users WHERE email = %s",
                (current_user["email"],)
            )
            row = cur.fetchone()
            cur.close()
            return {
                "preferred_agent": row[0] if row and row[0] else "langgraph",
                "output_thinking": bool(row[1]) if row and row[1] is not None else False,
            }
        finally:
            release_db_conn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/settings")
async def update_user_settings(req: SettingsUpdateRequest, current_user: dict = Depends(get_current_user)):
    if req.preferred_agent not in ("langgraph", "llamaindex"):
        raise HTTPException(status_code=400, detail="Invalid agent framework")
    try:
        conn = get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET preferred_agent = %s, output_thinking = %s WHERE email = %s",
                (req.preferred_agent, req.output_thinking, current_user["email"])
            )
            conn.commit()
            cur.close()
            return {
                "status": "success",
                "preferred_agent": req.preferred_agent,
                "output_thinking": req.output_thinking,
            }
        finally:
            release_db_conn(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
