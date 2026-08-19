"""
routes/crop_analysis.py
Backs the "AI Camera Scanner" screen:
  1. Client uploads an image of a crop.
  2. We store it (local disk here; swap for S3/Cloud Storage by changing
     `_save_upload` only) and run it through an AI inference abstraction.
  3. Result is persisted to cropanalysis, mirrored into history, and (when
     confidence is high enough) a recommendation + notification are created
     automatically so the Recommendation/Notification screens light up.
"""

import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query

from config import db_cursor, UPLOAD_DIR, MAX_UPLOAD_MB, logger
from schemas import ApiResponse, PaginatedResponse, CropAnalysisOut
from utils.deps import get_current_user, CurrentUser
from utils.pagination import paginate_params, build_meta

router = APIRouter()

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CONFIDENCE_RECOMMENDATION_THRESHOLD = 0.60


def _save_upload(file: UploadFile) -> str:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")

    file.file.seek(0, os.SEEK_END)
    size_mb = file.file.tell() / (1024 * 1024)
    file.file.seek(0)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB}MB limit")

    filename = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as out:
        out.write(file.file.read())
    return path


def _run_ai_inference(image_path: str) -> dict:
    """
    AI inference service abstraction.
    Swap this function's body for a real call to your model-serving
    endpoint (e.g. a TorchServe/TF-Serving/HuggingFace inference API).
    Keeping the interface stable means routes never change when the model
    backend changes.
    """
    # TODO: replace with a real HTTP call to the inference service, e.g.:
    # resp = httpx.post(AI_SERVICE_URL, files={"image": open(image_path, "rb")})
    # return resp.json()
    logger.info(f"[AI STUB] Running inference on {image_path}")
    return {"disease_detected": "Unknown - inference service not configured", "confidence_score": 0.0}


def _maybe_create_recommendation(cur, analysis_id: int, disease: Optional[str], confidence: Optional[float]):
    if not disease or confidence is None or confidence < CONFIDENCE_RECOMMENDATION_THRESHOLD:
        return None
    message = f"Detected '{disease}' with {confidence * 100:.1f}% confidence. Consider inspecting the affected area and applying appropriate treatment."
    cur.execute(
        '''INSERT INTO "recommendations" (analysis_id, recommendation_type, message)
           VALUES (%s, %s, %s) RETURNING recommendation_id''',
        (analysis_id, "crop_disease", message),
    )
    return cur.fetchone()["recommendation_id"]


@router.post("/upload", response_model=ApiResponse[CropAnalysisOut], status_code=201)
def upload_and_analyze(
    farm_id: int,
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(get_current_user),
):
    with db_cursor() as cur:
        cur.execute('SELECT farm_id FROM "farms" WHERE farm_id = %s AND user_id = %s', (farm_id, current_user.id))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Farm not found or not owned by user")

    image_path = _save_upload(file)
    result = _run_ai_inference(image_path)

    with db_cursor(commit=True) as cur:
        cur.execute(
            '''INSERT INTO "cropanalysis" (user_id, farm_id, image_path, disease_detected, confidence_score)
               VALUES (%s, %s, %s, %s, %s) RETURNING *''',
            (current_user.id, farm_id, image_path, result.get("disease_detected"), result.get("confidence_score")),
        )
        analysis = cur.fetchone()

        cur.execute(
            '''INSERT INTO "history" (user_id, farm_id, analysis_id, action_type)
               VALUES (%s, %s, %s, %s)''',
            (current_user.id, farm_id, analysis["analysis_id"], "crop_analysis_created"),
        )

        rec_id = _maybe_create_recommendation(
            cur, analysis["analysis_id"], result.get("disease_detected"), result.get("confidence_score")
        )
        if rec_id:
            cur.execute(
                '''INSERT INTO "notifications" (user_id, recommendation_id, title, body)
                   VALUES (%s, %s, %s, %s)''',
                (
                    current_user.id,
                    rec_id,
                    "New crop recommendation",
                    f"We found a possible issue on your farm: {result.get('disease_detected')}",
                ),
            )

    return ApiResponse(message="Analysis complete", data=CropAnalysisOut(**analysis))


@router.get("", response_model=PaginatedResponse[CropAnalysisOut])
def list_analyses(
    farm_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    page, page_size, offset = paginate_params(page, page_size)
    where = "WHERE user_id = %s"
    params = [current_user.id]
    if farm_id:
        where += " AND farm_id = %s"
        params.append(farm_id)

    with db_cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS total FROM "cropanalysis" {where}', params)
        total = cur.fetchone()["total"]
        cur.execute(
            f'SELECT * FROM "cropanalysis" {where} ORDER BY analyzed_at DESC LIMIT %s OFFSET %s',
            params + [page_size, offset],
        )
        rows = cur.fetchall()

    return PaginatedResponse(data=[CropAnalysisOut(**r) for r in rows], meta=build_meta(page, page_size, total))


@router.get("/{analysis_id}", response_model=ApiResponse[CropAnalysisOut])
def get_analysis(analysis_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            'SELECT * FROM "cropanalysis" WHERE analysis_id = %s AND user_id = %s',
            (analysis_id, current_user.id),
        )
        analysis = cur.fetchone()
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return ApiResponse(data=CropAnalysisOut(**analysis))
