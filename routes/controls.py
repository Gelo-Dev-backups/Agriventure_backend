"""
routes/controls.py
Backs the "Controls" screen: per-farm irrigation settings + connected
device (sensor) status. 1:1 port of agriventure_backedn_xampp's
ControlsController.

Irrigation control is currently OFF (see IRRIGATION_CONTROL_ENABLED in
config.py/.env) - there's no irrigation device/actuator yet, and
AgriVenture is monitoring + advice focused for now. The PATCH logic below
is fully implemented (updates the irrigation_settings row - mode,
is_active, schedule) and left in place, just gated off by that one flag,
so turning it on later is a one-line .env change, no code changes needed.

Scope note for whenever it IS turned on: PATCH updates real, persisted
software state - it does NOT drive any physical relay/valve itself, since
no ESP32 firmware or broker in this project currently reads these values
back to actuate hardware.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from config import db_cursor, IRRIGATION_CONTROL_ENABLED
from schemas import ApiResponse
from utils.deps import get_current_user, get_farm_owned_or_404, CurrentUser

router = APIRouter()

ALLOWED_MODES = ("automatic", "manual", "scheduled")


def _irrigation_settings_for(cur, farm_id: int) -> dict:
    """Returns the farm's irrigation_settings row, creating a default one on first access."""
    cur.execute('SELECT * FROM "irrigation_settings" WHERE farm_id = %s', (farm_id,))
    settings = cur.fetchone()
    if not settings:
        cur.execute('INSERT INTO "irrigation_settings" (farm_id) VALUES (%s)', (farm_id,))
        cur.execute('SELECT * FROM "irrigation_settings" WHERE farm_id = %s', (farm_id,))
        settings = cur.fetchone()
    return settings


def _device_out(s: dict) -> dict:
    return {
        "sensor_id": s["sensor_id"],
        "sensor_code": s["sensor_code"],
        "sensor_name": s["sensor_name"],
        "status": s["status"],
        "is_online": s["status"] == "Active",
        "battery_percent": s.get("battery_percent"),
        "last_seen_at": s.get("last_seen_at"),
    }


def _farm_out(farm: dict, settings: dict, devices: list) -> dict:
    return {
        "farm_id": farm["farm_id"],
        "farm_name": farm["farm_name"],
        "mode": settings["mode"],
        "is_active": bool(settings["is_active"]),
        "scheduled_time": settings.get("scheduled_time"),
        "duration_minutes": settings.get("duration_minutes"),
        "updated_at": settings.get("updated_at"),
        "irrigation_control_enabled": IRRIGATION_CONTROL_ENABLED,
        "devices": [_device_out(d) for d in devices],
    }


@router.get("", response_model=ApiResponse)
def list_controls(current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        cur.execute(
            '''SELECT farm_id, farm_name FROM "farms" WHERE user_id = %s AND is_archived = false
               ORDER BY created_at DESC''',
            (current_user.id,),
        )
        farms = cur.fetchall()

        result = []
        for farm in farms:
            farm_id = farm["farm_id"]
            settings = _irrigation_settings_for(cur, farm_id)
            cur.execute('SELECT * FROM "sensors" WHERE farm_id = %s ORDER BY registered_at', (farm_id,))
            devices = cur.fetchall()
            result.append(_farm_out(farm, settings, devices))

    return ApiResponse(data=result)


@router.patch("/{farm_id}/irrigation", response_model=ApiResponse)
def irrigation(
    farm_id: int,
    payload: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Updates a farm's irrigation_settings row. Body fields are all
    optional - only the ones present get changed.
    """
    get_farm_owned_or_404(farm_id, current_user.id)

    if not IRRIGATION_CONTROL_ENABLED:
        raise HTTPException(status_code=423, detail="Irrigation control is not available yet - no device connected")

    with db_cursor(commit=True) as cur:
        _irrigation_settings_for(cur, farm_id)  # lazily creates the row if missing

        fields = []
        params = []

        if "is_active" in payload:
            fields.append("is_active = %s")
            params.append(bool(payload["is_active"]))
        if "mode" in payload:
            mode = payload["mode"]
            if mode not in ALLOWED_MODES:
                raise HTTPException(status_code=422, detail=f"Field 'mode' must be one of: {', '.join(ALLOWED_MODES)}")
            fields.append("mode = %s")
            params.append(mode)
        if "scheduled_time" in payload:
            fields.append("scheduled_time = %s")
            params.append(payload["scheduled_time"])  # 'HH:MM:SS' or null to clear
        if "duration_minutes" in payload:
            fields.append("duration_minutes = %s")
            params.append(payload["duration_minutes"])

        if not fields:
            raise HTTPException(status_code=422, detail="No fields provided to update")

        cur.execute(f'UPDATE "irrigation_settings" SET {", ".join(fields)} WHERE farm_id = %s', params + [farm_id])

        cur.execute('SELECT farm_id, farm_name FROM "farms" WHERE farm_id = %s', (farm_id,))
        farm = cur.fetchone()

        settings = _irrigation_settings_for(cur, farm_id)
        cur.execute('SELECT * FROM "sensors" WHERE farm_id = %s ORDER BY registered_at', (farm_id,))
        devices = cur.fetchall()

    return ApiResponse(message="Irrigation settings updated", data=_farm_out(farm, settings, devices))
