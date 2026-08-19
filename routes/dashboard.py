"""
routes/dashboard.py
Single aggregate endpoint for the Flutter "DashboardScreen" (Index 0), so
the app doesn't need to fire five separate requests on launch: user info,
farm/sensor counts, latest IoT readings, recent AI analyses, recent
recommendations, and unread notification count all come back in one call.
"""

from fastapi import APIRouter, Depends

from config import db_cursor
from schemas import ApiResponse, DashboardSummary, UserOut, CropAnalysisOut, RecommendationOut
from utils.deps import get_current_user, CurrentUser

router = APIRouter()


@router.get("/summary", response_model=ApiResponse[DashboardSummary])
def get_dashboard_summary(current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            'SELECT id, full_name, email, is_verified, created_at FROM "users" WHERE id = %s',
            (current_user.id,),
        )
        user = cur.fetchone()

        cur.execute('SELECT COUNT(*) AS total FROM "farms" WHERE user_id = %s', (current_user.id,))
        total_farms = cur.fetchone()["total"]

        cur.execute(
            '''SELECT COUNT(*) AS total FROM "sensors" s
               JOIN "farms" f ON f.farm_id = s.farm_id WHERE f.user_id = %s''',
            (current_user.id,),
        )
        total_sensors = cur.fetchone()["total"]

        cur.execute(
            '''SELECT COUNT(*) AS total FROM "sensors" s
               JOIN "farms" f ON f.farm_id = s.farm_id
               WHERE f.user_id = %s AND s.status = 'Active' ''',
            (current_user.id,),
        )
        active_sensors = cur.fetchone()["total"]
        inactive_sensors = total_sensors - active_sensors

        cur.execute(
            'SELECT COUNT(*) AS total FROM "notifications" WHERE user_id = %s AND is_read = false',
            (current_user.id,),
        )
        unread_notifications = cur.fetchone()["total"]

        cur.execute(
            '''SELECT * FROM "cropanalysis" WHERE user_id = %s
               ORDER BY analyzed_at DESC LIMIT 5''',
            (current_user.id,),
        )
        recent_analyses = cur.fetchall()

        cur.execute(
            '''SELECT DISTINCT r.* FROM "recommendations" r
               LEFT JOIN "cropanalysis" ca ON ca.analysis_id = r.analysis_id
               LEFT JOIN "sensorreadings" sr ON sr.reading_id = r.reading_id
               LEFT JOIN "sensors" s ON s.sensor_id = sr.sensor_id
               LEFT JOIN "farms" fa ON fa.farm_id = ca.farm_id
               LEFT JOIN "farms" fs ON fs.farm_id = s.farm_id
               WHERE (fa.user_id = %s OR fs.user_id = %s)
               ORDER BY r.created_at DESC LIMIT 5''',
            (current_user.id, current_user.id),
        )
        recent_recommendations = cur.fetchall()

        cur.execute(
            '''SELECT DISTINCT ON (s.sensor_id)
                      s.sensor_id, s.sensor_name, s.farm_id,
                      r.temperature, r.humidity, r.soil_moisture, r.recorded_at
               FROM "sensors" s
               JOIN "farms" f ON f.farm_id = s.farm_id
               LEFT JOIN "sensorreadings" r ON r.sensor_id = s.sensor_id
               WHERE f.user_id = %s
               ORDER BY s.sensor_id, r.recorded_at DESC NULLS LAST
               LIMIT 20''',
            (current_user.id,),
        )
        latest_readings = cur.fetchall()

    summary = DashboardSummary(
        user=UserOut(**user),
        total_farms=total_farms,
        total_sensors=total_sensors,
        active_sensors=active_sensors,
        inactive_sensors=inactive_sensors,
        unread_notifications=unread_notifications,
        recent_analyses=[CropAnalysisOut(**a) for a in recent_analyses],
        recent_recommendations=[RecommendationOut(**r) for r in recent_recommendations],
        latest_readings=latest_readings,
    )
    return ApiResponse(data=summary)
