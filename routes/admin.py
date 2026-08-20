"""
routes/admin.py
Backs agriventure_admin - user/role management and IoT device-token
issuance. Every endpoint requires an admin/superadmin bearer token (see
utils.deps.require_admin/require_superadmin). 1:1 port of
agriventure_backedn_xampp's AdminController - same role model, same
guard rules, same response shapes (plain dicts, not tightly-typed response
models, matching the PHP version's approach and keeping this admin-only
surface easy to extend without churning schemas.py).

Role model:
  user       - mobile-app farmer account. The ONLY role /auth/register can
               ever create; public registration always defaults to it and
               this router never overrides that.
  iot        - device/sensor service account. No password login is
               expected to be used; instead the admin panel mints a
               long-lived static JWT (stored in users.iot_token) that gets
               flashed into the device/ESP32 firmware. It's a normal
               'access' JWT under the hood, so every existing
               get_current_user()-protected endpoint (sensor ingest
               included) accepts it unmodified.
  admin      - can manage users/devices from agriventure_admin.
  superadmin - everything admin can, plus create/demote other
               admins/superadmins and delete accounts.
"""

import secrets
from typing import Optional

import psycopg2.errors
from fastapi import APIRouter, Depends, HTTPException, Query

from config import db_cursor
from schemas import ApiResponse, PaginatedResponse, AdminCreateUserRequest, AdminUpdateUserRequest, AdminTokenRequest, _EMAIL_RE
from utils.deps import get_current_user, require_admin, require_superadmin, CurrentUser
from utils.pagination import paginate_params, build_meta
from utils.security import hash_password, create_iot_token

router = APIRouter()

ROLES = ("user", "iot", "admin", "superadmin")
ELEVATED_ROLES = ("admin", "superadmin")
EXPIRY_DAYS_CHOICES = (30, 90, 180, 365)


def _random_password() -> str:
    # Only ever used for 'iot' accounts, which authenticate via the static
    # device token, never via this password.
    return secrets.token_hex(16)


def _validate_account_email(email: str, is_iot: bool) -> str:
    """
    IoT/device accounts don't need a real inbox - the "email" is just a
    unique account identifier (e.g. "greenhouse-sensor-01") flashed into
    firmware, never a login destination for a human - so it's exempt from
    email-format validation. Every other role keeps the normal strict check.
    """
    if is_iot:
        return email.strip().lower()
    if not _EMAIL_RE.match(email.strip()):
        raise HTTPException(status_code=422, detail="Field 'email' must be a valid email address")
    return email.strip().lower()


def _normalize_expiry_days(raw) -> Optional[int]:
    if raw is None or raw == "" or raw == "never":
        return None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = None
    if days not in EXPIRY_DAYS_CHOICES:
        raise HTTPException(
            status_code=422,
            detail=f"Field 'expires_in_days' must be one of: {', '.join(map(str, EXPIRY_DAYS_CHOICES))}, or \"never\"",
        )
    return days


def _user_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "email": row["email"],
        "role": row["role"],
        "is_verified": bool(row["is_verified"]),
        "has_iot_token": bool(row.get("iot_token")),
        "iot_token_expires_at": row.get("iot_token_expires_at"),
        "created_at": row.get("created_at"),
    }


def _find_user_or_404(cur, user_id: int) -> dict:
    cur.execute('SELECT * FROM "users" WHERE id = %s', (user_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return row


def _issue_iot_token(cur, user_id: int, email: str, expiry_days: Optional[int]) -> dict:
    token, expires_at = create_iot_token(user_id, email, expiry_days)
    cur.execute(
        'UPDATE "users" SET iot_token = %s, iot_token_expires_at = %s WHERE id = %s',
        (token, expires_at, user_id),
    )
    return {"token": token, "expires_at": expires_at}


# ---------------------------------------------------------------------------
# GET /admin/stats
# ---------------------------------------------------------------------------
@router.get("/stats", response_model=ApiResponse)
def stats(current_user: CurrentUser = Depends(require_admin)):
    with db_cursor() as cur:
        cur.execute('SELECT role, COUNT(*) AS n FROM "users" GROUP BY role')
        counts = {"user": 0, "iot": 0, "admin": 0, "superadmin": 0}
        for row in cur.fetchall():
            counts[row["role"]] = row["n"]

        cur.execute('SELECT COUNT(*) AS n FROM "farms"')
        total_farms = cur.fetchone()["n"]
        cur.execute('SELECT COUNT(*) AS n FROM "sensors"')
        total_sensors = cur.fetchone()["n"]
        cur.execute('SELECT COUNT(*) AS n FROM "sensorreadings"')
        total_readings = cur.fetchone()["n"]
        cur.execute(
            '''SELECT COUNT(*) AS n FROM "users" WHERE role = 'iot' AND iot_token_expires_at IS NOT NULL
               AND iot_token_expires_at <= now() + INTERVAL '7 days' '''
        )
        expiring_soon = cur.fetchone()["n"]

    return ApiResponse(data={
        "users_by_role": counts,
        "total_farms": total_farms,
        "total_sensors": total_sensors,
        "total_readings": total_readings,
        "iot_tokens_expiring_soon": expiring_soon,
    })


# ---------------------------------------------------------------------------
# GET /admin/users?role=&q=&page=&page_size=
# ---------------------------------------------------------------------------
@router.get("/users", response_model=PaginatedResponse)
def list_users(
    role: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(require_admin),
):
    page, page_size, offset = paginate_params(page, page_size)

    where = []
    params = []
    if role and role in ROLES:
        where.append("role = %s")
        params.append(role)
    if q:
        where.append("(full_name ILIKE %s OR email ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with db_cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS n FROM "users" {where_sql}', params)
        total = cur.fetchone()["n"]

        cur.execute(
            f'SELECT * FROM "users" {where_sql} ORDER BY created_at DESC LIMIT %s OFFSET %s',
            params + [page_size, offset],
        )
        rows = cur.fetchall()

    return PaginatedResponse(data=[_user_out(r) for r in rows], meta=build_meta(page, page_size, total))


# ---------------------------------------------------------------------------
# GET /admin/users/{user_id}
# ---------------------------------------------------------------------------
@router.get("/users/{user_id}", response_model=ApiResponse)
def get_user(user_id: int, current_user: CurrentUser = Depends(require_admin)):
    with db_cursor() as cur:
        row = _find_user_or_404(cur, user_id)
    return ApiResponse(data=_user_out(row))


# ---------------------------------------------------------------------------
# POST /admin/users
# ---------------------------------------------------------------------------
@router.post("/users", response_model=ApiResponse, status_code=201)
def create_user(payload: AdminCreateUserRequest, current_user: CurrentUser = Depends(require_admin)):
    role = payload.role
    if role not in ROLES:
        raise HTTPException(status_code=422, detail=f"Field 'role' must be one of: {', '.join(ROLES)}")
    if role in ELEVATED_ROLES and not current_user.is_superadmin():
        raise HTTPException(status_code=403, detail="Only a superadmin can create admin/superadmin accounts")

    is_iot = role == "iot"
    email = _validate_account_email(payload.email, is_iot)
    password = (payload.password or _random_password()) if is_iot else payload.password
    if not is_iot and not password:
        raise HTTPException(status_code=422, detail="Field 'password' is required")

    with db_cursor() as cur:
        cur.execute('SELECT id FROM "users" WHERE email = %s', (email,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="An account with this email already exists")

    try:
        with db_cursor(commit=True) as cur:
            # Admin-panel-created accounts are pre-verified - there's no
            # self-serve OTP flow here, the admin already vetted the account.
            cur.execute(
                '''INSERT INTO "users" (full_name, email, password_hash, role, is_verified)
                   VALUES (%s, %s, %s, %s, true) RETURNING id''',
                (payload.fullName.strip(), email, hash_password(password), role),
            )
            user_id = cur.fetchone()["id"]

            iot_token = None
            if is_iot:
                expiry_days = _normalize_expiry_days(payload.expires_in_days)
                iot_token = _issue_iot_token(cur, user_id, email, expiry_days)
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    with db_cursor() as cur:
        row = _find_user_or_404(cur, user_id)

    return ApiResponse(
        message="Account created",
        data={
            "user": _user_out(row),
            "iot_token": iot_token["token"] if iot_token else None,
            "iot_token_expires_at": iot_token["expires_at"] if iot_token else None,
        },
    )


# ---------------------------------------------------------------------------
# PATCH /admin/users/{user_id}
# ---------------------------------------------------------------------------
@router.patch("/users/{user_id}", response_model=ApiResponse)
def update_user(user_id: int, payload: AdminUpdateUserRequest, current_user: CurrentUser = Depends(require_admin)):
    with db_cursor() as cur:
        target = _find_user_or_404(cur, user_id)

    fields = []
    params: list = []

    if payload.fullName is not None:
        fields.append("full_name = %s")
        params.append(payload.fullName.strip())

    if payload.role is not None:
        new_role = payload.role
        if new_role not in ROLES:
            raise HTTPException(status_code=422, detail=f"Field 'role' must be one of: {', '.join(ROLES)}")
        touches_elevated = new_role in ELEVATED_ROLES or target["role"] in ELEVATED_ROLES
        if touches_elevated and not current_user.is_superadmin():
            raise HTTPException(status_code=403, detail="Only a superadmin can grant or revoke admin/superadmin roles")
        if target["id"] == current_user.id and target["role"] == "superadmin" and new_role != "superadmin":
            raise HTTPException(status_code=400, detail="You can't demote your own superadmin account")
        fields.append("role = %s")
        params.append(new_role)

    if payload.is_verified is not None:
        fields.append("is_verified = %s")
        params.append(payload.is_verified)

    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")

    with db_cursor(commit=True) as cur:
        cur.execute(f'UPDATE "users" SET {", ".join(fields)} WHERE id = %s', params + [user_id])

    with db_cursor() as cur:
        row = _find_user_or_404(cur, user_id)

    return ApiResponse(message="Account updated", data=_user_out(row))


# ---------------------------------------------------------------------------
# GET /admin/users/{user_id}/token - re-display a stored IoT token
# ---------------------------------------------------------------------------
@router.get("/users/{user_id}/token", response_model=ApiResponse)
def get_token(user_id: int, current_user: CurrentUser = Depends(require_admin)):
    with db_cursor() as cur:
        row = _find_user_or_404(cur, user_id)
    if row["role"] != "iot":
        raise HTTPException(status_code=400, detail="Only IoT accounts have a device token")
    if not row.get("iot_token"):
        raise HTTPException(status_code=404, detail="No token has been generated for this device yet")
    return ApiResponse(data={"iot_token": row["iot_token"], "iot_token_expires_at": row.get("iot_token_expires_at")})


# ---------------------------------------------------------------------------
# POST /admin/users/{user_id}/token - regenerate
# ---------------------------------------------------------------------------
@router.post("/users/{user_id}/token", response_model=ApiResponse)
def regenerate_token(user_id: int, payload: AdminTokenRequest, current_user: CurrentUser = Depends(require_admin)):
    with db_cursor() as cur:
        row = _find_user_or_404(cur, user_id)
    if row["role"] != "iot":
        raise HTTPException(status_code=400, detail="Only IoT accounts have a device token")

    expiry_days = _normalize_expiry_days(payload.expires_in_days)
    with db_cursor(commit=True) as cur:
        token = _issue_iot_token(cur, user_id, row["email"], expiry_days)

    return ApiResponse(
        message="Token regenerated",
        data={"iot_token": token["token"], "iot_token_expires_at": token["expires_at"]},
    )


# ---------------------------------------------------------------------------
# DELETE /admin/users/{user_id}
# ---------------------------------------------------------------------------
@router.delete("/users/{user_id}", response_model=ApiResponse)
def delete_user(user_id: int, current_user: CurrentUser = Depends(require_superadmin)):
    with db_cursor() as cur:
        target = _find_user_or_404(cur, user_id)

    if target["id"] == current_user.id:
        raise HTTPException(status_code=400, detail="You can't delete your own account")

    if target["role"] == "superadmin":
        with db_cursor() as cur:
            cur.execute('''SELECT COUNT(*) AS n FROM "users" WHERE role = 'superadmin' ''')
            remaining = cur.fetchone()["n"]
        if remaining <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last remaining superadmin")

    # NOTE: deleting a 'user' account cascades (ON DELETE CASCADE) to their
    # farms/sensors/readings/etc - expected for a full account deletion,
    # but worth knowing before deleting a farmer's account rather than an
    # 'iot'/'admin' one.
    with db_cursor(commit=True) as cur:
        cur.execute('DELETE FROM "users" WHERE id = %s', (user_id,))

    return ApiResponse(message="Account deleted")
