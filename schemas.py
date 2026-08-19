"""
schemas.py
All Pydantic request / response models (DTOs) used across the API.
Keeping them centralized keeps controllers thin and gives Swagger/OpenAPI
docs a single source of truth.
"""

from datetime import datetime
from typing import Optional, List, Any, Generic, TypeVar
from pydantic import BaseModel, EmailStr, Field, ConfigDict

T = TypeVar("T")


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
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=4, max_length=6)


class ResendOtpRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
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
    email: EmailStr
    is_verified: bool
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


class SensorReadingCreate(BaseModel):
    sensor_code: str
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    soil_moisture: Optional[float] = None
    recorded_at: Optional[datetime] = None


class SensorReadingBulkCreate(BaseModel):
    readings: List[SensorReadingCreate]


class SensorReadingOut(BaseModel):
    reading_id: int
    sensor_id: int
    temperature: Optional[float]
    humidity: Optional[float]
    soil_moisture: Optional[float]
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
    recommendation_type: Optional[str]
    message: str
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
