"""
utils/deps.py
Reusable FastAPI dependencies - primarily the "who is calling me" guard
used by every protected route. Keeping it here (rather than duplicated in
each router) is what makes the authorization middleware consistent.
"""

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import db_cursor
from utils.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=True)


class CurrentUser:
    def __init__(self, id: int, email: str, full_name: str, is_verified: bool, role: str = "user"):
        self.id = id
        self.email = email
        self.full_name = full_name
        self.is_verified = is_verified
        self.role = role

    def is_admin(self) -> bool:
        return self.role in ("admin", "superadmin")

    def is_superadmin(self) -> bool:
        return self.role == "superadmin"


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> CurrentUser:
    token = credentials.credentials
    try:
        payload = decode_access_token(token)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id = int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Access token expired")
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid access token")

    with db_cursor() as cur:
        cur.execute(
            'SELECT id, email, full_name, is_verified, role FROM "users" WHERE id = %s',
            (user_id,),
        )
        user = cur.fetchone()

    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")

    return CurrentUser(
        id=user["id"],
        email=user["email"],
        full_name=user["full_name"],
        is_verified=user["is_verified"],
        role=user.get("role", "user"),
    )


def require_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Admin panel guard - 'admin' or 'superadmin' only."""
    if not current_user.is_admin():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


def require_superadmin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Guard for actions only the top tier may take (creating other admins, deleting accounts)."""
    if not current_user.is_superadmin():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superadmin access required")
    return current_user


def require_verified_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not current_user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please verify your account first.",
        )
    return current_user


def get_farm_owned_or_404(farm_id: int, user_id: int):
    """Shared ownership check reused by farms/sensors/analysis routes."""
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM "farms" WHERE farm_id = %s AND user_id = %s',
            (farm_id, user_id),
        )
        farm = cur.fetchone()
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found or not owned by user")
    return farm
