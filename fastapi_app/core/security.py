"""
Authentication and security utilities module.

This module provides essential security functions for the backend, including:
1. Securely loading secrets (supports Docker secrets via _FILE suffix).
2. Bcrypt password hashing and verification.
3. JWT (JSON Web Token) creation, decoding, and validation.
4. FastAPI dependency (`get_current_user`) to secure API routes.
"""

import os
import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

def get_secret(name: str) -> str:
    """
    Safely load a secret from an environment variable or a Docker secret file.
    
    If `{name}_FILE` is present in the environment and the file exists, 
    the secret is read from the file. Otherwise, it falls back to the environment variable.
    
    Args:
        name (str): The base name of the environment variable (e.g., 'JWT_SECRET').
        
    Returns:
        str: The loaded secret string.
    """
    file_path = os.getenv(f"{name}_FILE")
    if file_path and os.path.exists(file_path):
        return open(file_path).read().strip()
    return os.getenv(name)

# Ensure JWT secret is securely loaded before starting the application
SECRET_KEY = get_secret("JWT_SECRET")
if not SECRET_KEY:
    raise RuntimeError("FATAL: JWT_SECRET environment variable or JWT_SECRET_FILE is not set. Refusing to start.")

# Define JWT algorithm and token expiry duration
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# OAuth2 scheme used by FastAPI for interactive Swagger UI login and Bearer token parsing
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def hash_password(password: str) -> str:
    """
    Hash a plaintext password using Bcrypt with a randomly generated salt.
    """
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Securely verify a plaintext password against a Bcrypt hashed password.
    Catches exceptions to prevent timing or parsing attacks.
    """
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        return False

def get_current_user(token: str = Depends(oauth2_scheme)):
    """
    FastAPI Dependency: Validate the JWT Bearer token from the request.
    
    Decodes the JWT to extract the user's email (`sub` claim) and `role`.
    Raises a 401 HTTP exception if the token is missing, expired, or invalid.
    
    Returns:
        dict: A dictionary containing the authenticated user's email and role.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        role: str = payload.get("role")
        if email is None:
            raise credentials_exception
        return {"email": email, "role": role}
    except jwt.PyJWTError:
        raise credentials_exception
