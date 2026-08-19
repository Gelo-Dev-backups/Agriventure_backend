"""
utils/pagination.py
Small helper to keep pagination math (offset, total_pages) consistent
across every list endpoint.
"""

import math
from schemas import PaginationMeta


def paginate_params(page: int = 1, page_size: int = 20):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)  # hard cap to protect the DB
    offset = (page - 1) * page_size
    return page, page_size, offset


def build_meta(page: int, page_size: int, total_items: int) -> PaginationMeta:
    total_pages = math.ceil(total_items / page_size) if page_size else 0
    return PaginationMeta(
        page=page, page_size=page_size, total_items=total_items, total_pages=total_pages
    )
