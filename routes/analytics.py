"""
routes/analytics.py
Backs the "Analytics" screen: current per-metric readings plus a bucketed
time series (with min/avg/max) for soil moisture, temperature, humidity,
and battery percentage, aggregated server-side so the app never has to
pull raw readings and bucket thousands of rows on-device for a 30-day view.

Scope: always farm-wide by default (every sensor on the farm, combined -
"All Sensors" in the app), optionally narrowed to exactly one sensor via
?sensor_id=. Narrowing never widens: a given call either reflects the
whole farm or exactly one sensor on it, never a mix.

1:1 port of agriventure_backedn_xampp's AnalyticsController - the only
real difference is bucketing (Postgres date_trunc/to_char instead of
MySQL's DATE_FORMAT) and NOW()-INTERVAL syntax.
"""

from fastapi import APIRouter, Depends, Query, HTTPException

from config import db_cursor
from utils.deps import get_current_user, CurrentUser, get_farm_owned_or_404
from schemas import ApiResponse

router = APIRouter()

# interval: Postgres INTERVAL literal (hardcoded, never user-input - safe
# to splice). bucket_trunc: date_trunc() precision. bucket_format:
# to_char() format matching that precision.
_RANGES = {
    "24h": {"interval": "24 hours", "bucket_trunc": "hour", "bucket_format": "YYYY-MM-DD HH24:00:00"},
    "7d": {"interval": "7 days", "bucket_trunc": "day", "bucket_format": "YYYY-MM-DD"},
    "30d": {"interval": "30 days", "bucket_trunc": "day", "bucket_format": "YYYY-MM-DD"},
}
_METRICS = ["soil_moisture", "temperature", "humidity", "battery_percent"]


@router.get("/farm/{farm_id}", response_model=ApiResponse)
def farm_analytics(
    farm_id: int,
    range: str = Query("7d"),
    sensor_id: int | None = Query(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    get_farm_owned_or_404(farm_id, current_user.id)

    if range not in _RANGES:
        raise HTTPException(status_code=422, detail=f"Query parameter 'range' must be one of: {', '.join(_RANGES)}")
    config = _RANGES[range]

    # Optional device filter - "All Sensors" (default, farm-wide) vs.
    # exactly one sensor. A sensor_id that exists but belongs to a
    # different farm 404s rather than silently falling back to farm-wide,
    # so a bad/stale filter never quietly shows the wrong data instead of
    # an obvious error.
    if sensor_id is not None:
        with db_cursor() as cur:
            cur.execute('SELECT sensor_id FROM "sensors" WHERE sensor_id = %s AND farm_id = %s', (sensor_id, farm_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Sensor not found on this farm")

    sensor_clause = " AND r.sensor_id = %s" if sensor_id is not None else ""
    base_params = [farm_id] + ([sensor_id] if sensor_id is not None else [])

    current = _current_readings(base_params, sensor_clause)
    metrics = {metric: _metric_series(base_params, metric, config, sensor_clause) for metric in _METRICS}

    return ApiResponse(data={
        "farm_id": farm_id,
        "sensor_id": sensor_id,
        "range": range,
        "current": current,
        "metrics": metrics,
    })


def _round1(value):
    return round(float(value), 1) if value is not None else None


def _current_readings(base_params: list, sensor_clause: str) -> dict:
    with db_cursor() as cur:
        cur.execute(
            f'''SELECT AVG(soil_moisture) AS soil_moisture, AVG(temperature) AS temperature,
                       AVG(humidity) AS humidity, AVG(battery_percent) AS battery_percent
                FROM (
                    SELECT DISTINCT ON (r.sensor_id)
                           r.soil_moisture, r.temperature, r.humidity, r.battery_percent
                    FROM "sensorreadings" r
                    JOIN "sensors" s ON s.sensor_id = r.sensor_id
                    WHERE s.farm_id = %s{sensor_clause}
                    ORDER BY r.sensor_id, r.recorded_at DESC, r.reading_id DESC
                ) t''',
            base_params,
        )
        row = cur.fetchone()

    return {
        "soil_moisture": _round1(row["soil_moisture"]),
        "temperature": _round1(row["temperature"]),
        "humidity": _round1(row["humidity"]),
        "battery_percent": _round1(row["battery_percent"]),
    }


def _metric_series(base_params: list, metric: str, config: dict, sensor_clause: str) -> dict:
    # $metric is always one of the hardcoded _METRICS values (never
    # interpolated from user input), so splicing it into the column list
    # here is safe - same as the PHP version's equivalent comment.
    with db_cursor() as cur:
        cur.execute(
            f'''SELECT to_char(date_trunc(%s, r.recorded_at), %s) AS bucket, AVG(r.{metric}) AS value
                FROM "sensorreadings" r
                JOIN "sensors" s ON s.sensor_id = r.sensor_id
                WHERE s.farm_id = %s{sensor_clause} AND r.recorded_at >= now() - INTERVAL '{config["interval"]}'
                GROUP BY bucket
                ORDER BY bucket''',
            [config["bucket_trunc"], config["bucket_format"]] + base_params,
        )
        points = [{"bucket": r["bucket"], "value": _round1(r["value"])} for r in cur.fetchall()]

        cur.execute(
            f'''SELECT MIN(r.{metric}) AS min_value, AVG(r.{metric}) AS avg_value, MAX(r.{metric}) AS max_value
                FROM "sensorreadings" r
                JOIN "sensors" s ON s.sensor_id = r.sensor_id
                WHERE s.farm_id = %s{sensor_clause} AND r.recorded_at >= now() - INTERVAL '{config["interval"]}' ''',
            base_params,
        )
        summary = cur.fetchone()

    return {
        "points": points,
        "min": _round1(summary["min_value"]),
        "avg": _round1(summary["avg_value"]),
        "max": _round1(summary["max_value"]),
    }
