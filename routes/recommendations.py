"""
routes/recommendations.py
API for the "Advice" screen. Recommendations are created automatically
elsewhere - either by crop_analysis.py after AI inference, or by
utils/recommendation_engine.py after a sensor reading breaches a threshold
in "advice_rules" - so this router is about scoped, ownership-safe
retrieval and acknowledging cards.

Ownership is resolved three ways because recommendations arrive three ways:
older/crop-analysis rows only carry analysis_id/reading_id (farm is
derived via a JOIN chain), engine-created rows set farm_id directly. All
three are checked so nothing falls through - 1:1 port of
agriventure_backedn_xampp's RecommendationController.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from config import db_cursor
from schemas import ApiResponse, PaginatedResponse, RecommendationOut
from utils.deps import get_current_user, CurrentUser
from utils.pagination import paginate_params, build_meta

router = APIRouter()

_OWNERSHIP_JOINS = '''
    LEFT JOIN "cropanalysis" ca ON ca.analysis_id = r.analysis_id
    LEFT JOIN "sensorreadings" sr ON sr.reading_id = r.reading_id
    LEFT JOIN "sensors" s ON s.sensor_id = sr.sensor_id
    LEFT JOIN "farms" fa ON fa.farm_id = ca.farm_id
    LEFT JOIN "farms" fs ON fs.farm_id = s.farm_id
    LEFT JOIN "farms" fd ON fd.farm_id = r.farm_id
'''
_OWNERSHIP_WHERE = "(fa.user_id = %s OR fs.user_id = %s OR fd.user_id = %s)"


@router.get("", response_model=PaginatedResponse[RecommendationOut])
def list_recommendations(
    farm_id: Optional[int] = None,
    unacknowledged_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    page, page_size, offset = paginate_params(page, page_size)

    where_extra = ""
    params = [current_user.id, current_user.id, current_user.id]
    if farm_id:
        where_extra += " AND (ca.farm_id = %s OR s.farm_id = %s OR r.farm_id = %s)"
        params += [farm_id, farm_id, farm_id]
    if unacknowledged_only:
        where_extra += " AND r.is_acknowledged = false"

    query = f'''
        SELECT DISTINCT r.* FROM "recommendations" r
        {_OWNERSHIP_JOINS}
        WHERE {_OWNERSHIP_WHERE} {where_extra}
    '''

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM ({query}) sub", params)
        total = cur.fetchone()["total"]

        # Postgres (unlike MySQL) requires every ORDER BY expression to
        # appear in the SELECT list when DISTINCT is used - ordering by a
        # CASE derived from an already-selected column doesn't count. Wrap
        # in a subquery so the outer, non-DISTINCT ORDER BY is unrestricted.
        cur.execute(
            f'''SELECT * FROM ({query}) sub
                ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'maintenance' THEN 1 ELSE 2 END, created_at DESC
                LIMIT %s OFFSET %s''',
            params + [page_size, offset],
        )
        rows = cur.fetchall()

    return PaginatedResponse(data=[RecommendationOut(**r) for r in rows], meta=build_meta(page, page_size, total))


@router.get("/{recommendation_id}", response_model=ApiResponse[RecommendationOut])
def get_recommendation(recommendation_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            f'''SELECT DISTINCT r.* FROM "recommendations" r
                {_OWNERSHIP_JOINS}
                WHERE r.recommendation_id = %s AND {_OWNERSHIP_WHERE}''',
            (recommendation_id, current_user.id, current_user.id, current_user.id),
        )
        rec = cur.fetchone()
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return ApiResponse(data=RecommendationOut(**rec))


@router.patch("/{recommendation_id}/acknowledge", response_model=ApiResponse[RecommendationOut])
def acknowledge_recommendation(recommendation_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            f'''SELECT DISTINCT r.recommendation_id FROM "recommendations" r
                {_OWNERSHIP_JOINS}
                WHERE r.recommendation_id = %s AND {_OWNERSHIP_WHERE}''',
            (recommendation_id, current_user.id, current_user.id, current_user.id),
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Recommendation not found")

    with db_cursor(commit=True) as cur:
        cur.execute(
            '''UPDATE "recommendations" SET is_acknowledged = true, acknowledged_at = now()
               WHERE recommendation_id = %s RETURNING *''',
            (recommendation_id,),
        )
        rec = cur.fetchone()

    return ApiResponse(message="Recommendation acknowledged", data=RecommendationOut(**rec))
