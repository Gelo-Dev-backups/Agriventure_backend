"""
routes/notifications.py
Notification inbox for the user - read/unread tracking, mark-as-read.
Push/email/SMS dispatch is abstracted in utils/mailer.py (email) and can be
extended with an FCM/APNs client for real push notifications.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from config import db_cursor
from schemas import ApiResponse, PaginatedResponse, NotificationOut
from utils.deps import get_current_user, CurrentUser
from utils.pagination import paginate_params, build_meta

router = APIRouter()


@router.get("", response_model=PaginatedResponse[NotificationOut])
def list_notifications(
    unread_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
):
    page, page_size, offset = paginate_params(page, page_size)

    where = "WHERE user_id = %s"
    params = [current_user.id]
    if unread_only:
        where += " AND is_read = false"

    with db_cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS total FROM "notifications" {where}', params)
        total = cur.fetchone()["total"]
        cur.execute(
            f'SELECT * FROM "notifications" {where} ORDER BY sent_at DESC LIMIT %s OFFSET %s',
            params + [page_size, offset],
        )
        rows = cur.fetchall()

    return PaginatedResponse(data=[NotificationOut(**r) for r in rows], meta=build_meta(page, page_size, total))


@router.get("/unread-count", response_model=ApiResponse)
def unread_count(current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute(
            'SELECT COUNT(*) AS count FROM "notifications" WHERE user_id = %s AND is_read = false',
            (current_user.id,),
        )
        count = cur.fetchone()["count"]
    return ApiResponse(data={"unread_count": count})


@router.patch("/{notification_id}/read", response_model=ApiResponse[NotificationOut])
def mark_as_read(notification_id: int, current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        cur.execute(
            '''UPDATE "notifications" SET is_read = true
               WHERE notification_id = %s AND user_id = %s RETURNING *''',
            (notification_id, current_user.id),
        )
        notif = cur.fetchone()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    return ApiResponse(data=NotificationOut(**notif))


@router.patch("/read-all", response_model=ApiResponse)
def mark_all_as_read(current_user: CurrentUser = Depends(get_current_user)):
    with db_cursor(commit=True) as cur:
        cur.execute(
            'UPDATE "notifications" SET is_read = true WHERE user_id = %s AND is_read = false',
            (current_user.id,),
        )
        updated = cur.rowcount
    return ApiResponse(message=f"{updated} notifications marked as read")
