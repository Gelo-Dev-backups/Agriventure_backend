"""
routes/recommendations.py
Read-only API for the "Recommendation" screen. Recommendations are created
automatically elsewhere (crop_analysis.py after inference; a background
job could do the same for sensor-reading thresholds), so this router is
mostly about scoped, ownership-safe retrieval.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from config import db_cursor
from schemas import ApiResponse, PaginatedResponse, RecommendationOut
from utils.deps import get_current_user, CurrentUser
from utils.pagination import paginate_params, build_meta

router = APIRouter()


@router.get("", response_model=PaginatedResponse[RecommendationOut])
def list_recommendations(
    farm_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    page, page_size, offset = paginate_params(page, page_size)

    # A recommendation belongs to the user either via a crop analysis they
    # own, or via a sensor reading whose sensor sits on one of their farms.
    where_extra = ""
    params = [current_user.id, current_user.id]
    if farm_id:
        where_extra = "AND (ca.farm_id = %s OR s.farm_id = %s)"
        params += [farm_id, farm_id]

    query = f'''
        SELECT DISTINCT r.* FROM "recommendations" r
        LEFT JOIN "cropanalysis" ca ON ca.analysis_id = r.analysis_id
        LEFT JOIN "sensorreadings" sr ON sr.reading_id = r.reading_id
        LEFT JOIN "sensors" s ON s.sensor_id = sr.sensor_id
        LEFT JOIN "farms" fa ON fa.farm_id = ca.farm_id
        LEFT JOIN "farms" fs ON fs.farm_id = s.farm_id
        WHERE (fa.user_id = %s OR fs.user_id = %s) {where_extra}
    '''

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM ({query}) sub", params)
        total = cur.fetchone()["total"]

        cur.execute(f"{query} ORDER BY r.created_at DESC LIMIT %s OFFSET %s", params + [page_size, offset])
        rows = cur.fetchall()

    return PaginatedResponse(data=[RecommendationOut(**r) for r in rows], meta=build_meta(page, page_size, total))


@router.get("/{recommendation_id}", response_model=ApiResponse[RecommendationOut])
def get_recommendation(recommendation_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            '''SELECT DISTINCT r.* FROM "recommendations" r
               LEFT JOIN "cropanalysis" ca ON ca.analysis_id = r.analysis_id
               LEFT JOIN "sensorreadings" sr ON sr.reading_id = r.reading_id
               LEFT JOIN "sensors" s ON s.sensor_id = sr.sensor_id
               LEFT JOIN "farms" fa ON fa.farm_id = ca.farm_id
               LEFT JOIN "farms" fs ON fs.farm_id = s.farm_id
               WHERE r.recommendation_id = %s AND (fa.user_id = %s OR fs.user_id = %s)''',
            (recommendation_id, current_user.id, current_user.id),
        )
        rec = cur.fetchone()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return ApiResponse(data=RecommendationOut(**rec))
