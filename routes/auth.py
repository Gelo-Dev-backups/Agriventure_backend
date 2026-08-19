"""
routes/auth.py
Full authentication lifecycle for the "users" table:
  register -> verify OTP -> login -> refresh -> forgot/reset password -> logout

Design notes:
  - Passwords hashed with bcrypt (never stored/compared in plaintext).
  - Access tokens are short-lived; refresh tokens are long-lived and signed
    with a SEPARATE secret so a leaked access token can't be used to mint
    new refresh tokens.
  - Login/OTP endpoints are rate-limited to slow down brute-force / OTP
    spam attacks.
  - Every response uses the shared ApiResponse envelope for a predictable
    frontend contract (Flutter, web, etc. all parse the same shape).
"""

from datetime import datetime, timezone

import psycopg2.errors
from fastapi import APIRouter, HTTPException, Depends, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from config import db_cursor, logger
from schemas import (
    ApiResponse,
    RegisterRequest,
    LoginRequest,
    VerifyOtpRequest,
    ResendOtpRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    RefreshTokenRequest,
    ChangePasswordRequest,
    TokenPair,
    UserOut,
)
from utils.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    generate_otp,
    otp_expiry,
    is_otp_expired,
)
from utils.mailer import send_otp_email, send_welcome_email
from utils.deps import get_current_user, CurrentUser

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def _log_system_event(user_id, action: str, request: Request):
    try:
        ip = get_remote_address(request)
        with db_cursor(commit=True) as cur:
            cur.execute(
                'INSERT INTO "systemlogs" (user_id, action, ip_address) VALUES (%s, %s, %s)',
                (user_id, action, ip),
            )
    except Exception as e:  # audit logging must never break the main flow
        logger.warning(f"Failed to write system log: {e}")


@router.post("/register", response_model=ApiResponse)
@limiter.limit("5/minute")
def register(request: Request, payload: RegisterRequest):
    with db_cursor() as cur:
        cur.execute('SELECT id FROM "users" WHERE email = %s', (payload.email.lower(),))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="An account with this email already exists")

    otp = generate_otp()
    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                '''INSERT INTO "users" (full_name, email, password_hash, otp, otp_expires_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id, full_name, email, is_verified, created_at''',
                (
                    payload.fullName.strip(),
                    payload.email.lower().strip(),
                    hash_password(payload.password),
                    otp,
                    otp_expiry(),
                ),
            )
            user = cur.fetchone()
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    send_otp_email(payload.email, otp, purpose="registration")
    _log_system_event(user["id"], "user_registered", request)

    return ApiResponse(
        message="Registration successful. Please verify your email with the OTP we sent.",
        data={"email": user["email"]},
    )


@router.post("/verify-otp", response_model=ApiResponse[TokenPair])
@limiter.limit("10/minute")
def verify_otp(request: Request, payload: VerifyOtpRequest):
    """Verifies the OTP and, on success, logs the user straight in (same
    token pair shape as /login) so the app can auto-login right after
    registration instead of bouncing back to the login screen."""
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM "users" WHERE email = %s', (payload.email.lower(),)
        )
        user = cur.fetchone()

    if not user:
        raise HTTPException(status_code=404, detail="Account not found")

    if not user["is_verified"]:
        if user["otp"] != payload.otp or is_otp_expired(user["otp_expires_at"]):
            raise HTTPException(status_code=400, detail="Invalid or expired OTP")

        with db_cursor(commit=True) as cur:
            cur.execute(
                '''UPDATE "users" SET is_verified = true, otp = NULL, otp_expires_at = NULL
                   WHERE id = %s RETURNING id, full_name, email, is_verified, created_at''',
                (user["id"],),
            )
            user = cur.fetchone()

        _log_system_event(user["id"], "email_verified", request)
        send_welcome_email(user["email"], user["full_name"])

    access_token = create_access_token(user["id"], user["email"])
    refresh_token = create_refresh_token(user["id"], user["email"])
    _log_system_event(user["id"], "login_success", request)

    return ApiResponse(
        message="Email verified successfully",
        data=TokenPair(access_token=access_token, refresh_token=refresh_token, user=UserOut(**user)),
    )


@router.post("/resend-otp", response_model=ApiResponse)
@limiter.limit("3/minute")
def resend_otp(request: Request, payload: ResendOtpRequest):
    with db_cursor() as cur:
        cur.execute('SELECT id, is_verified FROM "users" WHERE email = %s', (payload.email.lower(),))
        user = cur.fetchone()

    if not user:
        # Do not reveal whether the account exists.
        return ApiResponse(message="If that account exists, a new OTP has been sent.")
    if user["is_verified"]:
        return ApiResponse(message="Account already verified.")

    otp = generate_otp()
    with db_cursor(commit=True) as cur:
        cur.execute(
            'UPDATE "users" SET otp = %s, otp_expires_at = %s WHERE id = %s',
            (otp, otp_expiry(), user["id"]),
        )
    send_otp_email(payload.email, otp, purpose="resend")
    return ApiResponse(message="If that account exists, a new OTP has been sent.")


@router.post("/login", response_model=ApiResponse[TokenPair])
@limiter.limit("8/minute")
def login(request: Request, credentials: LoginRequest):
    with db_cursor() as cur:
        cur.execute('SELECT * FROM "users" WHERE email = %s', (credentials.email.lower().strip(),))
        user = cur.fetchone()

    # Constant-shaped error to avoid leaking which part (email/password) was wrong.
    invalid_creds = HTTPException(status_code=401, detail="Invalid email or password")

    if not user:
        raise invalid_creds

    if not verify_password(credentials.password, user["password_hash"]):
        _log_system_event(user["id"], "login_failed", request)
        raise invalid_creds

    if not user["is_verified"]:
        raise HTTPException(status_code=403, detail="Please verify your email before logging in")

    access_token = create_access_token(user["id"], user["email"])
    refresh_token = create_refresh_token(user["id"], user["email"])
    _log_system_event(user["id"], "login_success", request)

    return ApiResponse(
        message="Login successful",
        data=TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            user=UserOut(**user),
        ),
    )


@router.post("/refresh", response_model=ApiResponse[TokenPair])
@limiter.limit("20/minute")
def refresh_token_endpoint(request: Request, payload: RefreshTokenRequest):
    try:
        decoded = decode_refresh_token(payload.refresh_token)
        if decoded.get("type") != "refresh":
            raise ValueError("wrong token type")
        user_id = int(decoded["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    with db_cursor() as cur:
        cur.execute('SELECT * FROM "users" WHERE id = %s', (user_id,))
        user = cur.fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")

    # Refresh token rotation: issue a brand new pair every time.
    new_access = create_access_token(user["id"], user["email"])
    new_refresh = create_refresh_token(user["id"], user["email"])

    return ApiResponse(
        message="Token refreshed",
        data=TokenPair(access_token=new_access, refresh_token=new_refresh, user=UserOut(**user)),
    )


@router.post("/forgot-password", response_model=ApiResponse)
@limiter.limit("3/minute")
def forgot_password(request: Request, payload: ForgotPasswordRequest):
    with db_cursor() as cur:
        cur.execute('SELECT id FROM "users" WHERE email = %s', (payload.email.lower(),))
        user = cur.fetchone()

    if user:
        otp = generate_otp()
        with db_cursor(commit=True) as cur:
            cur.execute(
                'UPDATE "users" SET otp = %s, otp_expires_at = %s WHERE id = %s',
                (otp, otp_expiry(), user["id"]),
            )
        send_otp_email(payload.email, otp, purpose="password_reset")

    # Same response whether or not the account exists (prevents enumeration).
    return ApiResponse(message="If that account exists, a password reset OTP has been sent.")


@router.post("/reset-password", response_model=ApiResponse)
@limiter.limit("5/minute")
def reset_password(request: Request, payload: ResetPasswordRequest):
    with db_cursor() as cur:
        cur.execute('SELECT * FROM "users" WHERE email = %s', (payload.email.lower(),))
        user = cur.fetchone()

    if not user or user["otp"] != payload.otp or is_otp_expired(user["otp_expires_at"]):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    with db_cursor(commit=True) as cur:
        cur.execute(
            '''UPDATE "users" SET password_hash = %s, otp = NULL, otp_expires_at = NULL
               WHERE id = %s''',
            (hash_password(payload.new_password), user["id"]),
        )

    _log_system_event(user["id"], "password_reset", request)
    return ApiResponse(message="Password has been reset. You can now log in.")


@router.post("/change-password", response_model=ApiResponse)
def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    with db_cursor() as cur:
        cur.execute('SELECT password_hash FROM "users" WHERE id = %s', (current_user.id,))
        row = cur.fetchone()

    if not verify_password(payload.old_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Old password is incorrect")

    with db_cursor(commit=True) as cur:
        cur.execute(
            'UPDATE "users" SET password_hash = %s WHERE id = %s',
            (hash_password(payload.new_password), current_user.id),
        )
    _log_system_event(current_user.id, "password_changed", request)
    return ApiResponse(message="Password changed successfully")


@router.post("/logout", response_model=ApiResponse)
def logout(request: Request, current_user: CurrentUser = Depends(get_current_user)):
    # Access/refresh tokens are stateless JWTs. True server-side revocation
    # would require a token-blacklist table (e.g. a `revoked_tokens` table
    # keyed by jti) - add one if you need hard logout-everywhere semantics.
    _log_system_event(current_user.id, "logout", request)
    return ApiResponse(message="Logged out. Please discard your tokens client-side.")


@router.get("/me", response_model=ApiResponse[UserOut])
def get_profile(current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            'SELECT id, full_name, email, is_verified, created_at FROM "users" WHERE id = %s',
            (current_user.id,),
        )
        user = cur.fetchone()
    return ApiResponse(data=UserOut(**user))