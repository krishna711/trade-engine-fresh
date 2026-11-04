"""
Authentication and Role-Based Access Control Module
Provides zero-dependency HMAC-SHA256 signed access tokens, password hashing/verification,
and FastAPI dependencies for user session management (ADMIN vs USER).
"""

import os
import time
import json
import hmac
import hashlib
import base64
import secrets
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session
from backend.database.connection import get_db
from backend.database.models import User

# Engine secret key for signing session tokens
SECRET_KEY = os.getenv("ENGINE_SECRET_KEY", "fresh-trade-engine-secret-key-2026-quantzen-xyz")
DEFAULT_TOKEN_EXPIRY_SECONDS = 86400 * 7  # 7 days


def hash_user_password(password: str) -> str:
    """Generate salted SHA-256 password hash"""
    salt = secrets.token_hex(8)
    h = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
    return f"{salt}${h}"


def verify_user_password(password: str, hashed: str) -> bool:
    """Verify password against stored hash (supports salted $ format and plain sha256)"""
    if not hashed:
        return False
    if "$" in hashed:
        salt, h = hashed.split("$", 1)
        return hashlib.sha256((salt + password).encode('utf-8')).hexdigest() == h
    return hashlib.sha256(password.encode('utf-8')).hexdigest() == hashed


def create_access_token(user: User, expires_in_seconds: int = DEFAULT_TOKEN_EXPIRY_SECONDS) -> str:
    """Create a tamper-proof HMAC-SHA256 signed session token"""
    payload = {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "exp": int(time.time()) + expires_in_seconds
    }
    payload_json = json.dumps(payload, separators=(',', ':'))
    b64_payload = base64.urlsafe_b64encode(payload_json.encode('utf-8')).decode('utf-8').rstrip('=')
    sig = hmac.new(SECRET_KEY.encode('utf-8'), b64_payload.encode('utf-8'), hashlib.sha256).hexdigest()
    return f"{b64_payload}.{sig}"


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode HMAC-SHA256 signed session token"""
    if not token or "." not in token:
        return None
    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            return None
        b64_payload, sig = parts
        expected_sig = hmac.new(SECRET_KEY.encode('utf-8'), b64_payload.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        
        # Restore base64 padding
        rem = len(b64_payload) % 4
        if rem > 0:
            b64_payload += "=" * (4 - rem)
        
        payload_json = base64.urlsafe_b64decode(b64_payload.encode('utf-8')).decode('utf-8')
        data = json.loads(payload_json)
        
        # Check token expiration
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def extract_token_from_request(request: Request) -> Optional[str]:
    """Extract session token from Authorization header, X-Auth-Token, query params, or cookie"""
    # 1. Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    # 2. X-Auth-Token header
    token = request.headers.get("X-Auth-Token")
    if token:
        return token.strip()

    # 3. Query parameter ?token=...
    token = request.query_params.get("token")
    if token:
        return token.strip()

    # 4. Cookie fresh_trade_auth_token
    token = request.cookies.get("fresh_trade_auth_token")
    if token:
        return token.strip()

    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency: Authenticates user or raises 401 Unauthorized"""
    token = extract_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in to access Fresh Trade Engine."
        )

    payload = decode_access_token(token)
    if not payload or "user_id" not in payload:
        raise HTTPException(
            status_code=401,
            detail="Session has expired or is invalid. Please log in again."
        )

    user = db.query(User).filter(User.id == payload["user_id"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="User account not found.")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is deactivated. Contact Administrator.")

    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """FastAPI dependency: Returns User if valid token present, else None (no 401 thrown)"""
    token = extract_token_from_request(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or "user_id" not in payload:
        return None
    user = db.query(User).filter(User.id == payload["user_id"]).first()
    if user and user.is_active:
        return user
    return None


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency: Enforces that current user must have ADMIN role"""
    if current_user.role != "ADMIN":
        raise HTTPException(
            status_code=403,
            detail="Administrator permissions required to perform this action."
        )
    return current_user
