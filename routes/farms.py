"""
routes/farms.py
CRUD for the "farms" resource. Every read/write is scoped to the
authenticated user (ownership check) so one user can never see or modify
another user's farms.

Sensor counts and health status are computed at query time from the
"sensors"/"sensorreadings" tables rather than stored on "farms" - avoids
duplicated, staleness-prone state (see migrations/002_add_farm_geo_columns.sql).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from config import db_cursor
from schemas import ApiResponse, PaginatedResponse, FarmCreate, FarmUpdate, FarmOut
from utils.deps import get_current_user, CurrentUser
from utils.pagination import paginate_params, build_meta

router = APIRouter()

# Soil-moisture thresholds (%) used to classify farm health from the most
# recent reading per sensor, averaged across the farm's sensors.
_CRITICAL_MOISTURE = 25
_WARNING_MOISTURE = 40


def _health_status(total_sensors: int, avg_moisture: Optional[float]) -> str:
    if total_sensors == 0 or avg_moisture is None:
        return "unknown"
    if avg_moisture < _CRITICAL_MOISTURE:
        return "critical"
    if avg_moisture < _WARNING_MOISTURE:
        return "warning"
    return "healthy"


def _attach_stats(cur, farms: list) -> list:
    """Merges live sensor_count/active_sensor_count/health_status onto each
    farm row. Single grouped query - no N+1."""
    if not farms:
        return []

    farm_ids = [f["farm_id"] for f in farms]
    cur.execute(
        '''SELECT s.farm_id,
                  COUNT(*) AS total_sensors,
                  COUNT(*) FILTER (WHERE s.status = 'Active') AS active_sensors,
                  AVG(lr.soil_moisture) AS avg_moisture
           FROM "sensors" s
           LEFT JOIN LATERAL (
               SELECT soil_moisture FROM "sensorreadings"
               WHERE sensor_id = s.sensor_id
               ORDER BY recorded_at DESC LIMIT 1
           ) lr ON true
           WHERE s.farm_id = ANY(%s)
           GROUP BY s.farm_id''',
        (farm_ids,),
    )
    stats_by_farm = {row["farm_id"]: row for row in cur.fetchall()}

    results = []
    for farm in farms:
        stats = stats_by_farm.get(farm["farm_id"])
        total = stats["total_sensors"] if stats else 0
        active = stats["active_sensors"] if stats else 0
        avg_moisture = stats["avg_moisture"] if stats else None
        results.append(
            FarmOut(
                **farm,
                sensor_count=total,
                active_sensor_count=active,
                health_status=_health_status(total, avg_moisture),
            )
        )
    return results


@router.post("", response_model=ApiResponse[FarmOut], status_code=201)
def create_farm(payload: FarmCreate, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        cur.execute(
            '''INSERT INTO "farms" (user_id, farm_name, crop_type, boundary_coordinates,
                                     farm_size, image_url)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *''',
            (
                current_user.id,
                payload.farm_name,
                payload.crop_type,
                payload.boundary_coordinates,
                payload.farm_size,
                payload.image_url,
            ),
        )
        farm = cur.fetchone()
    return ApiResponse(message="Farm created", data=FarmOut(**farm))


@router.get("", response_model=PaginatedResponse[FarmOut])
def list_farms(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    include_archived: bool = False,
    current_user: CurrentUser = Depends(get_current_user),
):
    page, page_size, offset = paginate_params(page, page_size)

    where = 'WHERE user_id = %s'
    params = [current_user.id]
    if not include_archived:
        where += " AND is_archived = false"
    if search:
        where += " AND farm_name ILIKE %s"
        params.append(f"%{search}%")

    with db_cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS total FROM "farms" {where}', params)
        total = cur.fetchone()["total"]

        cur.execute(
            f'''SELECT * FROM "farms" {where}
                ORDER BY created_at DESC LIMIT %s OFFSET %s''',
            params + [page_size, offset],
        )
        rows = cur.fetchall()
        farms_out = _attach_stats(cur, rows)

    return PaginatedResponse(data=farms_out, meta=build_meta(page, page_size, total))


@router.get("/{farm_id}", response_model=ApiResponse[FarmOut])
def get_farm(farm_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM "farms" WHERE farm_id = %s AND user_id = %s',
            (farm_id, current_user.id),
        )
        farm = cur.fetchone()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        farm_out = _attach_stats(cur, [farm])[0]
    return ApiResponse(data=farm_out)


@router.patch("/{farm_id}", response_model=ApiResponse[FarmOut])
def update_farm(
    farm_id: int, payload: FarmUpdate, current_user: CurrentUser = Depends(get_current_user)
):
    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    set_clause = ", ".join(f'{key} = %s' for key in updates.keys())
    params = list(updates.values()) + [farm_id, current_user.id]

    with db_cursor(commit=True) as cur:
        cur.execute(
            f'''UPDATE "farms" SET {set_clause}
                WHERE farm_id = %s AND user_id = %s RETURNING *''',
            params,
        )
        farm = cur.fetchone()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        farm_out = _attach_stats(cur, [farm])[0]

    return ApiResponse(message="Farm updated", data=farm_out)


@router.patch("/{farm_id}/archive", response_model=ApiResponse[FarmOut])
def archive_farm(farm_id: int, current_user: CurrentUser = Depends(get_current_user)):
    """Soft-delete: hides the farm from list_farms without touching its
    sensors/readings/analyses. Kept as its own hard-coded statement (like
    create_farm) rather than going through update_farm's dynamic SET
    builder, which blows up if a field without a matching column is sent."""
    with db_cursor(commit=True) as cur:
        cur.execute(
            '''UPDATE "farms" SET is_archived = true
               WHERE farm_id = %s AND user_id = %s RETURNING *''',
            (farm_id, current_user.id),
        )
        farm = cur.fetchone()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        farm_out = _attach_stats(cur, [farm])[0]
    return ApiResponse(message="Farm archived", data=farm_out)


@router.patch("/{farm_id}/restore", response_model=ApiResponse[FarmOut])
def restore_farm(farm_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        cur.execute(
            '''UPDATE "farms" SET is_archived = false
               WHERE farm_id = %s AND user_id = %s RETURNING *''',
            (farm_id, current_user.id),
        )
        farm = cur.fetchone()
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        farm_out = _attach_stats(cur, [farm])[0]
    return ApiResponse(message="Farm restored", data=farm_out)


@router.delete("/{farm_id}", response_model=ApiResponse)
def delete_farm(farm_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        cur.execute(
            'DELETE FROM "farms" WHERE farm_id = %s AND user_id = %s RETURNING farm_id',
            (farm_id, current_user.id),
        )
        deleted = cur.fetchone()

    if not deleted:
        raise HTTPException(status_code=404, detail="Farm not found")
    # ON DELETE CASCADE in the schema takes care of sensors/analyses/etc.
    return ApiResponse(message="Farm and all related data deleted")
