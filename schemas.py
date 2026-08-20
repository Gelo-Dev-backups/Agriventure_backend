"""
schemas.py
All Pydantic request / response models (DTOs) used across the API.
Keeping them centralized keeps controllers thin and gives Swagger/OpenAPI
docs a single source of truth.
"""

import re
from datetime import datetime
from typing import Optional, List, Any, Generic, TypeVar, Annotated
from pydantic import BaseModel, Field, ConfigDict, BeforeValidator

T = TypeVar("T")

# Deliberately NOT pydantic's EmailStr: the installed email-validator
# (2.3.0) unconditionally rejects RFC 6761 special-use TLDs like .local -
# no flag disables it (checked syntax.py directly) - but this project uses
# .local/.internal addresses throughout (the seeded superadmin account is
# literally superadmin@agriventure.local), and PHP's filter_var-based
# validator (agriventure_backedn_xampp) never had this restriction. This
# mirrors PHP's actual leniency: structurally valid local@domain.tld
# shape, no reserved-domain rejection.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_lenient_email(v: str) -> str:
    v = v.strip()
    if not _EMAIL_RE.match(v):
        raise ValueError("value is not a valid email address")
    return v.lower()


LenientEmail = Annotated[str, BeforeValidator(_validate_lenient_email)]


# ---------------------------------------------------------------------------
# Generic envelope - every endpoint responds with this consistent shape
# ---------------------------------------------------------------------------
class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str = "OK"
    data: Optional[T] = None


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int


class PaginatedResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str = "OK"
    data: List[T]
    meta: PaginationMeta


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    fullName: str = Field(min_length=2, max_length=100)
    email: LenientEmail
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: LenientEmail
    password: str


class VerifyOtpRequest(BaseModel):
    email: LenientEmail
    otp: str = Field(min_length=4, max_length=6)


class ResendOtpRequest(BaseModel):
    email: LenientEmail


class ForgotPasswordRequest(BaseModel):
    email: LenientEmail


class ResetPasswordRequest(BaseModel):
    email: LenientEmail
    otp: str
    new_password: str = Field(min_length=8, max_length=72)


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=72)


class UserOut(BaseModel):
    id: int
    full_name: str
    # Plain str, not LenientEmail: this is an OUTPUT field echoing back
    # whatever's already stored, not new input to validate. IoT accounts
    # are allowed non-email identifiers (e.g. "greenhouse-sensor-01", no
    # @ at all - see routes/admin.py's _validate_account_email), and
    # get_current_user()/auth/me must be able to return one's profile
    # without re-validating a value that was never meant to look like an
    # email in the first place. Same principle as LenientEmail itself:
    # validate once at the boundary where data enters, not every read.
    email: str
    is_verified: bool
    # Additive field - existing mobile-app consumers ignore it. Always
    # 'user' for accounts created via public /auth/register; 'admin'/
    # 'superadmin'/'iot' only exist via the admin panel (routes/admin.py).
    role: str = "user"
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------------------------------------------------------------------------
# Farms
# ---------------------------------------------------------------------------
class FarmCreate(BaseModel):
    farm_name: str = Field(min_length=1, max_length=100)
    crop_type: Optional[str] = "Cauliflower"
    boundary_coordinates: Optional[str] = None
    farm_size: Optional[float] = None
    image_url: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class FarmUpdate(BaseModel):
    farm_name: Optional[str] = None
    crop_type: Optional[str] = None
    boundary_coordinates: Optional[str] = None
    farm_size: Optional[float] = None
    image_url: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class FarmOut(BaseModel):
    farm_id: int
    user_id: int
    farm_name: str
    crop_type: Optional[str]
    boundary_coordinates: Optional[str]
    farm_size: Optional[float]
    created_at: Optional[datetime]
    image_url: Optional[str]
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_archived: bool = False
    # Computed at query time (not stored columns) - see routes/farms.py
    sensor_count: int = 0
    active_sensor_count: int = 0
    health_status: str = "unknown"


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------
class SensorCreate(BaseModel):
    farm_id: int
    sensor_code: str = Field(min_length=1, max_length=50)
    sensor_type: Optional[str] = "Soil/Temp/Humidity"
    sensor_name: Optional[str] = "New Sensor"
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class SensorUpdate(BaseModel):
    sensor_name: Optional[str] = None
    sensor_type: Optional[str] = None
    status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class SensorOut(BaseModel):
    sensor_id: int
    farm_id: int
    sensor_code: str
    sensor_type: Optional[str]
    status: Optional[str]
    registered_at: Optional[datetime]
    sensor_name: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    battery_percent: Optional[int] = None
    last_seen_at: Optional[datetime] = None
    # Only present when the caller joined latest-reading data (see
    # list_sensors's LEFT JOIN) - absent/null on plain sensor rows
    # (get/update/delete), which is fine since these all default to None.
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    soil_moisture: Optional[float] = None


class SensorReadingCreate(BaseModel):
    sensor_code: str
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    soil_moisture: Optional[float] = None
    battery_percent: Optional[int] = None
    recorded_at: Optional[datetime] = None


class SensorReadingBulkCreate(BaseModel):
    # Deliberately loose (raw dicts, not List[SensorReadingCreate]): one
    # malformed item must produce a per-item error in the response, not a
    # blanket 422 that rejects FastAPI/pydantic's own automatic validation
    # would otherwise give the whole batch before route logic even runs.
    # See ingest_readings_bulk's manual field-by-field validation.
    readings: List[dict]


class SensorReadingOut(BaseModel):
    reading_id: int
    sensor_id: int
    temperature: Optional[float]
    humidity: Optional[float]
    soil_moisture: Optional[float]
    battery_percent: Optional[int] = None
    recorded_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Crop analysis
# ---------------------------------------------------------------------------
class CropAnalysisCreate(BaseModel):
    farm_id: int
    image_path: str
    disease_detected: Optional[str] = None
    confidence_score: Optional[float] = None


class CropAnalysisOut(BaseModel):
    analysis_id: int
    user_id: int
    farm_id: int
    image_path: str
    disease_detected: Optional[str]
    confidence_score: Optional[float]
    analyzed_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------
class RecommendationCreate(BaseModel):
    reading_id: Optional[int] = None
    analysis_id: Optional[int] = None
    recommendation_type: Optional[str] = None
    message: str


class RecommendationOut(BaseModel):
    recommendation_id: int
    reading_id: Optional[int]
    analysis_id: Optional[int]
    farm_id: Optional[int] = None
    recommendation_type: Optional[str]
    message: str
    priority: str = "info"
    source_reference: Optional[str] = None
    recommended_action: Optional[str] = None
    is_acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    created_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
class NotificationOut(BaseModel):
    notification_id: int
    user_id: int
    recommendation_id: Optional[int]
    title: str
    body: str
    is_read: bool
    sent_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class DashboardSummary(BaseModel):
    user: UserOut
    total_farms: int
    total_sensors: int
    active_sensors: int
    inactive_sensors: int
    unread_notifications: int
    recent_analyses: List[CropAnalysisOut]
    recent_recommendations: List[RecommendationOut]
    latest_readings: List[Any]


# ---------------------------------------------------------------------------
# Admin (routes/admin.py - backs agriventure_admin, same role model as the
# PHP backend's AdminController)
# ---------------------------------------------------------------------------
class AdminCreateUserRequest(BaseModel):
    fullName: str = Field(min_length=2, max_length=100)
    # Deliberately str, not EmailStr: IoT/device accounts don't need a real
    # inbox - the "email" is just a unique identifier (e.g.
    # "greenhouse-sensor-01"), never a login destination for a human. Every
    # other role still gets a real email-format check, done manually in the
    # route body (see routes/admin.py's _validate_account_email).
    email: str = Field(min_length=1, max_length=255)
    role: str
    password: Optional[str] = Field(default=None, min_length=8, max_length=72)
    # One of 30/90/180/365, or "never" - iot role only.
    expires_in_days: Optional[Any] = None


class AdminUpdateUserRequest(BaseModel):
    fullName: Optional[str] = Field(default=None, min_length=2, max_length=100)
    role: Optional[str] = None
    is_verified: Optional[bool] = None


class AdminTokenRequest(BaseModel):
    expires_in_days: Optional[Any] = None


class AdminUserOut(BaseModel):
    id: int
    full_name: str
    email: str
    role: str
    is_verified: bool
    has_iot_token: bool
    iot_token_expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
