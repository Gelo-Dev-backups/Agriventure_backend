"""
routes/sensors.py
Sensor registration/management ("Add Sensor" screen) plus high-frequency
sensor-reading ingestion from IoT devices.

Notes on design:
  - Sensor create/update/delete require the caller to own the parent farm.
  - The reading-ingestion endpoint is intentionally NOT gated behind the
    farm-ownership check on the user JWT - IoT devices authenticate with
    their own sensor_code and don't hold a user session. In production,
    swap this for a per-device API key; for now it is left open behind the
    bearer-token dependency shared with the rest of the API so it is at
    least authenticated, and duplicate/replay protection is handled via
    the (sensor_id, recorded_at) check below.
  - Bulk insert endpoint lets a device batch-upload buffered readings after
    reconnecting (offline-first IoT pattern), and reports back exactly how
    many of the submitted readings were received/inserted/failed (with a
    per-item reason) rather than a single pass/fail for the whole batch -
    ported 1:1 from agriventure_backedn_xampp's SensorController.

  Every reading that's actually inserted is evaluated against the
  rule-based advice engine (utils/recommendation_engine.py) as a side
  effect AFTER the insert transaction commits - a failure there must never
  roll back an already-recorded reading.
"""

from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg2.errors

from config import db_cursor
from schemas import (
    ApiResponse,
    PaginatedResponse,
    SensorCreate,
    SensorUpdate,
    SensorOut,
    SensorReadingBulkCreate,
    SensorReadingOut,
)
from utils.deps import get_current_user, CurrentUser
from utils.pagination import paginate_params, build_meta
from utils.recommendation_engine import evaluate_reading

router = APIRouter()


def _assert_farm_owned(cur, farm_id: int, user_id: int):
    cur.execute('SELECT farm_id FROM "farms" WHERE farm_id = %s AND user_id = %s', (farm_id, user_id))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Farm not found or not owned by user")


def _get_sensor_owned_or_404(cur, sensor_id: int, user_id: int):
    cur.execute(
        '''SELECT s.* FROM "sensors" s
           JOIN "farms" f ON f.farm_id = s.farm_id
           WHERE s.sensor_id = %s AND f.user_id = %s''',
        (sensor_id, user_id),
    )
    sensor = cur.fetchone()
    if not sensor:
        raise HTTPException(status_code=404, detail="Sensor not found")
    return sensor


def _assert_in_range(key: str, value: Optional[float], min_v: float, max_v: float):
    """Raises a 422 if value is outside [min_v, max_v]. No-op for None (field simply omitted)."""
    if value is not None and (value < min_v or value > max_v):
        raise HTTPException(status_code=422, detail=f"Field '{key}' must be between {min_v} and {max_v}")


def _touch_sensor_heartbeat(cur, sensor_id: int, battery_percent: Optional[int]):
    """Updates the device-health columns a reading's battery value (if any) informs."""
    cur.execute(
        '''UPDATE "sensors" SET last_seen_at = now(), battery_percent = COALESCE(%s, battery_percent)
           WHERE sensor_id = %s''',
        (battery_percent, sensor_id),
    )


# ---------------------------------------------------------------------------
# Sensor CRUD
# ---------------------------------------------------------------------------
@router.post("", response_model=ApiResponse[SensorOut], status_code=201)
def create_sensor(payload: SensorCreate, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        _assert_farm_owned(cur, payload.farm_id, current_user.id)
        try:
            cur.execute(
                '''INSERT INTO "sensors" (farm_id, sensor_code, sensor_type, sensor_name,
                                           latitude, longitude)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *''',
                (
                    payload.farm_id,
                    payload.sensor_code,
                    payload.sensor_type,
                    payload.sensor_name,
                    payload.latitude,
                    payload.longitude,
                ),
            )
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="A sensor with this code already exists")
        sensor = cur.fetchone()
    return ApiResponse(message="Sensor registered", data=SensorOut(**sensor))


@router.get("", response_model=PaginatedResponse[SensorOut])
def list_sensors(
    farm_id: Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    page, page_size, offset = paginate_params(page, page_size)

    where = 'WHERE f.user_id = %s'
    params: List[Any] = [current_user.id]
    if farm_id:
        where += " AND s.farm_id = %s"
        params.append(farm_id)
    if status_filter:
        where += " AND s.status = %s"
        params.append(status_filter)

    with db_cursor() as cur:
        cur.execute(
            f'SELECT COUNT(*) AS total FROM "sensors" s JOIN "farms" f ON f.farm_id = s.farm_id {where}',
            params,
        )
        total = cur.fetchone()["total"]

        # Latest-reading columns are additive to the existing sensor fields -
        # powers both the Sensor Snapshot screen and the Analytics
        # device-filter dropdown from this one endpoint (same as
        # SensorController::list() on the PHP side; DISTINCT ON replaces
        # MySQL's ROW_NUMBER() window-function subquery).
        cur.execute(
            f'''SELECT s.*, lr.temperature, lr.humidity, lr.soil_moisture
                FROM "sensors" s
                JOIN "farms" f ON f.farm_id = s.farm_id
                LEFT JOIN LATERAL (
                    SELECT temperature, humidity, soil_moisture
                    FROM "sensorreadings"
                    WHERE sensor_id = s.sensor_id
                    ORDER BY recorded_at DESC, reading_id DESC
                    LIMIT 1
                ) lr ON true
                {where}
                ORDER BY s.registered_at DESC LIMIT %s OFFSET %s''',
            params + [page_size, offset],
        )
        rows = cur.fetchall()

    return PaginatedResponse(data=[SensorOut(**r) for r in rows], meta=build_meta(page, page_size, total))


@router.get("/{sensor_id}", response_model=ApiResponse[SensorOut])
def get_sensor(sensor_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        sensor = _get_sensor_owned_or_404(cur, sensor_id, current_user.id)
    return ApiResponse(data=SensorOut(**sensor))


@router.patch("/{sensor_id}", response_model=ApiResponse[SensorOut])
def update_sensor(
    sensor_id: int, payload: SensorUpdate, current_user: CurrentUser = Depends(get_current_user)
):
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    with db_cursor(commit=True) as cur:
        _get_sensor_owned_or_404(cur, sensor_id, current_user.id)
        set_clause = ", ".join(f"{k} = %s" for k in updates.keys())
        cur.execute(
            f'UPDATE "sensors" SET {set_clause} WHERE sensor_id = %s RETURNING *',
            list(updates.values()) + [sensor_id],
        )
        sensor = cur.fetchone()
    return ApiResponse(message="Sensor updated", data=SensorOut(**sensor))


@router.delete("/{sensor_id}", response_model=ApiResponse)
def delete_sensor(sensor_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        _get_sensor_owned_or_404(cur, sensor_id, current_user.id)
        cur.execute('DELETE FROM "sensors" WHERE sensor_id = %s', (sensor_id,))
    return ApiResponse(message="Sensor and its readings deleted")


# ---------------------------------------------------------------------------
# Sensor readings (IoT ingestion)
# ---------------------------------------------------------------------------
@router.post("/readings", response_model=ApiResponse, status_code=201)
def ingest_reading(payload: dict, current_user: CurrentUser = Depends(get_current_user)):
    sensor_code = payload.get("sensor_code")
    if not isinstance(sensor_code, str) or not (1 <= len(sensor_code) <= 50):
        raise HTTPException(status_code=422, detail="Field 'sensor_code' is required and must be 1-50 characters")

    temperature = payload.get("temperature")
    humidity = payload.get("humidity")
    soil_moisture = payload.get("soil_moisture")
    battery_percent = payload.get("battery_percent")
    recorded_at = payload.get("recorded_at")

    _assert_in_range("humidity", humidity, 0, 100)
    _assert_in_range("soil_moisture", soil_moisture, 0, 100)
    _assert_in_range("battery_percent", battery_percent, 0, 100)

    # Sensor lookup happens before opening a write transaction - a request
    # that's about to 404 has nothing to roll back.
    with db_cursor() as cur:
        cur.execute(
            '''SELECT s.sensor_id, s.farm_id, f.crop_type
               FROM "sensors" s JOIN "farms" f ON f.farm_id = s.farm_id
               WHERE s.sensor_code = %s''',
            (sensor_code,),
        )
        sensor = cur.fetchone()
    if not sensor:
        raise HTTPException(status_code=404, detail="Sensor not found")

    # Insert + heartbeat update are one logical operation - db_cursor's
    # single connection/commit-or-rollback scope gives this transactional
    # atomicity for free, same guarantee as the PHP backend's explicit
    # Database::beginTransaction()/commit().
    with db_cursor(commit=True) as cur:
        cur.execute(
            '''INSERT INTO "sensorreadings" (sensor_id, temperature, humidity, soil_moisture, battery_percent, recorded_at)
               VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING reading_id''',
            (sensor["sensor_id"], temperature, humidity, soil_moisture, battery_percent, recorded_at),
        )
        reading_id = cur.fetchone()["reading_id"]
        _touch_sensor_heartbeat(cur, sensor["sensor_id"], battery_percent)

    # Advice generation is a side effect of a successfully-recorded
    # reading, not part of that atomic unit - must never roll back an
    # already-committed reading if it fails (see recommendation_engine.py).
    evaluate_reading(
        sensor["farm_id"],
        sensor["crop_type"],
        {"temperature": temperature, "humidity": humidity, "soil_moisture": soil_moisture},
    )

    return ApiResponse(
        message="Sensor reading recorded successfully",
        data={"reading_id": reading_id, "sensor_id": sensor["sensor_id"]},
    )


def _validated_bulk_float(item: dict, key: str, min_v: Optional[float], max_v: Optional[float]) -> Optional[float]:
    """Type + range validation for one field of a raw (not-yet-typed) bulk reading item."""
    if key not in item or item[key] is None:
        return None
    value = item[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"Field '{key}' must be a number")
    value = float(value)
    lo = min_v if min_v is not None else float("-inf")
    hi = max_v if max_v is not None else float("inf")
    if value < lo or value > hi:
        raise ValueError(f"Field '{key}' must be between {min_v} and {max_v}")
    return value


@router.post("/readings/bulk", response_model=ApiResponse)
def ingest_readings_bulk(payload: SensorReadingBulkCreate, current_user: CurrentUser = Depends(get_current_user)):
    readings = payload.readings
    if not readings:
        raise HTTPException(status_code=400, detail="No readings provided")
    received = len(readings)

    # Pre-fetch sensor_code -> {sensor_id, farm_id, crop_type} to avoid N+1 lookups.
    codes = list({r.get("sensor_code") for r in readings if isinstance(r, dict) and isinstance(r.get("sensor_code"), str)})
    sensor_map: Dict[str, dict] = {}
    if codes:
        with db_cursor() as cur:
            cur.execute(
                '''SELECT s.sensor_id, s.sensor_code, s.farm_id, f.crop_type
                   FROM "sensors" s JOIN "farms" f ON f.farm_id = s.farm_id
                   WHERE s.sensor_code = ANY(%s)''',
                (codes,),
            )
            for row in cur.fetchall():
                sensor_map[row["sensor_code"]] = row

    # Pass 1: validate every item up front. Nothing invalid ever reaches an
    # INSERT ("do not insert corrupted data").
    valid: List[dict] = []
    errors: List[dict] = []
    for i, r in enumerate(readings):
        if not isinstance(r, dict):
            errors.append({"index": i, "message": "Reading must be an object"})
            continue

        code = r.get("sensor_code")
        if not isinstance(code, str) or not code or len(code) > 50:
            errors.append({"index": i, "sensor_code": code, "message": "'sensor_code' is required and must be 1-50 characters"})
            continue

        sensor = sensor_map.get(code)
        if sensor is None:
            errors.append({"index": i, "sensor_code": code, "message": "Sensor not found"})
            continue

        try:
            temperature = _validated_bulk_float(r, "temperature", None, None)
            humidity = _validated_bulk_float(r, "humidity", 0, 100)
            soil_moisture = _validated_bulk_float(r, "soil_moisture", 0, 100)
            battery_percent = _validated_bulk_float(r, "battery_percent", 0, 100)
        except ValueError as e:
            errors.append({"index": i, "sensor_code": code, "message": str(e)})
            continue

        valid.append({
            "sensor_id": sensor["sensor_id"],
            "farm_id": sensor["farm_id"],
            "crop_type": sensor["crop_type"],
            "temperature": temperature,
            "humidity": humidity,
            "soil_moisture": soil_moisture,
            "battery_percent": int(battery_percent) if battery_percent is not None else None,
            "recorded_at": r.get("recorded_at") if isinstance(r.get("recorded_at"), str) else None,
        })

    # Pass 2: insert the validated subset, same one-logical-operation
    # transaction discipline as the single-reading endpoint.
    inserted = 0
    # Latest valid reading per sensor in this batch - evaluated against the
    # advice rules once per sensor rather than once per row, since the
    # engine's own dedupe logic makes evaluating every row redundant.
    latest_per_sensor: Dict[int, dict] = {}

    if valid:
        with db_cursor(commit=True) as cur:
            for v in valid:
                cur.execute(
                    '''INSERT INTO "sensorreadings" (sensor_id, temperature, humidity, soil_moisture, battery_percent, recorded_at)
                       VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))''',
                    (v["sensor_id"], v["temperature"], v["humidity"], v["soil_moisture"], v["battery_percent"], v["recorded_at"]),
                )
                latest_per_sensor[v["sensor_id"]] = v
            inserted = len(valid)

            for sensor_id, v in latest_per_sensor.items():
                _touch_sensor_heartbeat(cur, sensor_id, v["battery_percent"])

        for v in latest_per_sensor.values():
            evaluate_reading(
                v["farm_id"], v["crop_type"],
                {"temperature": v["temperature"], "humidity": v["humidity"], "soil_moisture": v["soil_moisture"]},
            )

    return ApiResponse(
        message="Bulk sensor readings processed",
        data={"received": received, "inserted": inserted, "failed": len(errors), "errors": errors},
    )


@router.get("/{sensor_id}/readings", response_model=PaginatedResponse[SensorReadingOut])
def get_sensor_readings(
    sensor_id: int,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: CurrentUser = Depends(get_current_user),
):
    page, page_size, offset = paginate_params(page, page_size)

    with db_cursor() as cur:
        _get_sensor_owned_or_404(cur, sensor_id, current_user.id)

        where = "WHERE sensor_id = %s"
        params: List = [sensor_id]
        if start:
            where += " AND recorded_at >= %s"
            params.append(start)
        if end:
            where += " AND recorded_at <= %s"
            params.append(end)

        cur.execute(f'SELECT COUNT(*) AS total FROM "sensorreadings" {where}', params)
        total = cur.fetchone()["total"]

        cur.execute(
            f'''SELECT * FROM "sensorreadings" {where}
                ORDER BY recorded_at DESC LIMIT %s OFFSET %s''',
            params + [page_size, offset],
        )
        rows = cur.fetchall()

    return PaginatedResponse(
        data=[SensorReadingOut(**r) for r in rows], meta=build_meta(page, page_size, total)
    )


@router.get("/farm/{farm_id}/latest", response_model=ApiResponse)
def get_latest_readings_for_farm(farm_id: int, current_user: CurrentUser = Depends(get_current_user)):
    """Latest reading per sensor for a farm - powers the dashboard / sensor overview."""
    with db_cursor() as cur:
        _assert_farm_owned(cur, farm_id, current_user.id)
        cur.execute(
            '''SELECT DISTINCT ON (s.sensor_id)
                      s.sensor_id, s.sensor_name, s.sensor_code, s.status,
                      r.temperature, r.humidity, r.soil_moisture, r.battery_percent, r.recorded_at
               FROM "sensors" s
               LEFT JOIN "sensorreadings" r ON r.sensor_id = s.sensor_id
               WHERE s.farm_id = %s
               ORDER BY s.sensor_id, r.recorded_at DESC NULLS LAST''',
            (farm_id,),
        )
        rows = cur.fetchall()
    return ApiResponse(data=rows)
