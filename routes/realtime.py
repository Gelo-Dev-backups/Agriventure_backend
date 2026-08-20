"""
routes/realtime.py
Server-Sent Events stream for live (no-refresh) updates: latest sensor
readings and unread notification count for one farm.

Port of agriventure_backedn_xampp's RealtimeController, minus the
irrigation section - there is no irrigation_settings table in this backend
(Controls/irrigation was deliberately left out of this port; see
migrations/004_role_battery_advice_engine.sql's docblock). Add it back
here if Controls ever gets ported too.

Auth is checked once, up front, before the stream opens - this is a
long-lived connection, not a per-message request, so there's nothing to
re-check later. The generator self-terminates after MAX_STREAM_SECONDS so
it never pins a worker forever; the Flutter client
(lib/core/services/realtime_service.dart) reconnects with backoff whenever
the connection drops, including on this clean end-of-stream.
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from config import db_cursor
from utils.deps import get_current_user, CurrentUser, get_farm_owned_or_404

router = APIRouter()

POLL_INTERVAL_SECONDS = 5
MAX_STREAM_SECONDS = 600  # 10 minutes, then the client reconnects


def _snapshot(farm_id: int, user_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute(
            '''SELECT DISTINCT ON (s.sensor_id)
                      s.sensor_id, s.sensor_name, s.sensor_code, s.status, s.battery_percent,
                      r.temperature, r.humidity, r.soil_moisture, r.recorded_at
               FROM "sensors" s
               LEFT JOIN "sensorreadings" r ON r.sensor_id = s.sensor_id
               WHERE s.farm_id = %s
               ORDER BY s.sensor_id, r.recorded_at DESC NULLS LAST''',
            (farm_id,),
        )
        readings = cur.fetchall()

        cur.execute(
            'SELECT COUNT(*) AS total FROM "notifications" WHERE user_id = %s AND is_read = false',
            (user_id,),
        )
        unread = cur.fetchone()["total"]

    return {
        "farm_id": farm_id,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "readings": readings,
        "unread_notifications": unread,
    }


def _default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


async def _event_stream(farm_id: int, user_id: int):
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < MAX_STREAM_SECONDS:
        payload = _snapshot(farm_id, user_id)
        yield f"data: {json.dumps(payload, default=_default)}\n\n"

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        yield ": heartbeat\n\n"


@router.get("/farm/{farm_id}")
def farm_stream(farm_id: int, current_user: CurrentUser = Depends(get_current_user)):
    get_farm_owned_or_404(farm_id, current_user.id)

    return StreamingResponse(
        _event_stream(farm_id, current_user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
