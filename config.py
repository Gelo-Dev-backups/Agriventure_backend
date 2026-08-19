"""
config.py
Central configuration for the AgriVenture backend:
 - environment variable loading
 - PostgreSQL connection pooling (thread-safe, reused across requests)
 - JWT / security settings
 - small shared constants
"""

import os
import logging
from contextlib import contextmanager

from dotenv import load_dotenv
from psycopg2 import pool
import psycopg2.extras

load_dotenv()

# ---------------------------------------------------------------------------
# App / environment
# ---------------------------------------------------------------------------
APP_NAME = os.getenv("APP_NAME", "AgriVenture Backend")
ENV = os.getenv("ENV", "production")
IS_PROD = ENV.lower() == "production"

# ---------------------------------------------------------------------------
# Logging (structured-ish, request IDs are attached in main.py middleware)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO if IS_PROD else logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(APP_NAME)

# ---------------------------------------------------------------------------
# JWT / security settings
# ---------------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET", "dev_only_change_me")
JWT_REFRESH_SECRET = os.getenv("JWT_REFRESH_SECRET", "dev_only_change_me_too")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
OTP_EXPIRE_MINUTES = int(os.getenv("OTP_EXPIRE_MINUTES", "10"))

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
_cors_raw = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = ["*"] if _cors_raw.strip() == "*" else [o.strip() for o in _cors_raw.split(",")]

# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads/crop_images")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Database connection pool
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.warning("DATABASE_URL is not set. The API will fail on first DB call.")

_MIN_CONN = int(os.getenv("DB_POOL_MIN", "0"))
_MAX_CONN = int(os.getenv("DB_POOL_MAX", "10"))

_connection_pool = None
if DATABASE_URL:
    _connection_pool = pool.ThreadedConnectionPool(_MIN_CONN, _MAX_CONN, dsn=DATABASE_URL)


def get_db_connection():
    """
    Returns a raw connection from the pool.
    Caller is responsible for closing/releasing it (use release_db_connection
    or, preferably, the `db_cursor()` context manager below).
    """
    if _connection_pool is None:
        raise RuntimeError("Database pool is not initialized. Check DATABASE_URL.")
    conn = _connection_pool.getconn()
    conn.autocommit = False
    return conn


def release_db_connection(conn):
    if _connection_pool is not None and conn is not None:
        _connection_pool.putconn(conn)


@contextmanager
def db_cursor(commit: bool = False):
    """
    Preferred way to talk to the DB:

        with db_cursor(commit=True) as cur:
            cur.execute("INSERT INTO farms (...) VALUES (...)", (...))

    Handles connection checkout/return, commit/rollback and cursor closing
    automatically, and always returns rows as dicts (RealDictCursor).
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        release_db_connection(conn)


def close_pool():
    if _connection_pool is not None:
        _connection_pool.closeall()
