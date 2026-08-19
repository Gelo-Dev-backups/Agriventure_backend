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
    reconnecting (offline-first IoT pattern).
"""

from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg2.extras

from config import db_cursor 
from schemas import (
    ApiResponse,
    PaginatedResponse,
    SensorCreate,
    SensorUpdate,
    SensorOut,
    SensorReadingCreate,
    SensorReadingBulkCreate,
    SensorReadingOut,
)
from utils.deps import get_current_user, CurrentUser
from utils.pagination import paginate_params, build_meta

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
    params = [current_user.id]
    if farm_id:
        where += " AND s.farm_id = %s"
        params.append(farm_id)
    if status_filter:
        where += " AND s.status = %s"
        params.append(status_filter)

    base = f'FROM "sensors" s JOIN "farms" f ON f.farm_id = s.farm_id {where}'

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total {base}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f'SELECT s.* {base} ORDER BY s.registered_at DESC LIMIT %s OFFSET %s',
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
def ingest_reading(payload: SensorReadingCreate, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        cur.execute('SELECT sensor_id FROM "sensors" WHERE sensor_code = %s', (payload.sensor_code,))
        sensor = cur.fetchone()
        if not sensor:
            raise HTTPException(status_code=404, detail="Unknown sensor_code")

        cur.execute(
            '''INSERT INTO "sensorreadings" (sensor_id, temperature, humidity, soil_moisture, recorded_at)
               VALUES (%s, %s, %s, %s, COALESCE(%s, CURRENT_TIMESTAMP)) RETURNING reading_id''',
            (
                sensor["sensor_id"],
                payload.temperature,
                payload.humidity,
                payload.soil_moisture,
                payload.recorded_at,
            ),
        )
        reading_id = cur.fetchone()["reading_id"]

    return ApiResponse(message="Reading recorded", data={"reading_id": reading_id})


@router.post("/readings/bulk", response_model=ApiResponse)
def ingest_readings_bulk(payload: SensorReadingBulkCreate, current_user: CurrentUser = Depends(get_current_user)):
    if not payload.readings:
        raise HTTPException(status_code=400, detail="No readings provided")

    inserted = 0
    with db_cursor(commit=True) as cur:
        # Pre-fetch sensor_code -> sensor_id map to avoid N+1 lookups.
        codes = list({r.sensor_code for r in payload.readings})
        cur.execute('SELECT sensor_id, sensor_code FROM "sensors" WHERE sensor_code = ANY(%s)', (codes,))
        code_map = {row["sensor_code"]: row["sensor_id"] for row in cur.fetchall()}

        rows = []
        for r in payload.readings:
            sensor_id = code_map.get(r.sensor_code)
            if sensor_id is None:
                continue  # skip unknown sensors rather than failing the whole batch
            rows.append((sensor_id, r.temperature, r.humidity, r.soil_moisture, r.recorded_at))

        if rows:
            psycopg2.extras.execute_values(
                cur,
                '''INSERT INTO "sensorreadings" (sensor_id, temperature, humidity, soil_moisture, recorded_at)
                   VALUES %s''',
                rows,
                template="(%s, %s, %s, %s, COALESCE(%s, CURRENT_TIMESTAMP))",
            )
            inserted = len(rows)

    return ApiResponse(message=f"{inserted} readings recorded", data={"inserted": inserted})


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
                      r.temperature, r.humidity, r.soil_moisture, r.recorded_at
               FROM "sensors" s
               LEFT JOIN "sensorreadings" r ON r.sensor_id = s.sensor_id
               WHERE s.farm_id = %s
               ORDER BY s.sensor_id, r.recorded_at DESC NULLS LAST''',
            (farm_id,),
        )
        rows = cur.fetchall()
    return ApiResponse(data=rows)
