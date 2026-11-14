"""HTTP 接口层。"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .models import BatchRequest, BatchResult, ConflictResponse, DailyReport
from .service import ReadingService, RevisionConflictError

logger = logging.getLogger(__name__)

router = APIRouter()


def get_service(request: Request) -> ReadingService:
    return request.app.state.service


@router.get("/healthz", summary="健康检查")
def healthz() -> dict:
    return {"status": "ok"}


@router.post(
    "/api/v1/readings/batch",
    response_model=BatchResult,
    status_code=200,
    summary="批量补报 / 修订 / 撤回 PM2.5 小时值",
    responses={
        409: {
            "model": ConflictResponse,
            "description": "同一站点日期小时修订号已存在但内容不同；整批已回滚。",
        },
        422: {"description": "请求体校验失败。"},
    },
)
def submit_batch(
    payload: BatchRequest,
    service: ReadingService = Depends(get_service),
) -> BatchResult:
    """原子写入一批记录。

    * 每小时以最大 revision 为准；新 revision 追加，不覆盖历史。
    * 同 revision 同内容重复提交无副作用（幂等）。
    * 同 revision 不同内容 → 409，整批回滚，不留下部分写入。
    """
    try:
        outcome = service.apply_batch(payload.records)
    except RevisionConflictError as exc:
        body = ConflictResponse(
            message="one or more revisions already exist with different content; batch rolled back",
            conflicts=exc.conflicts,
        )
        raise HTTPException(status_code=409, detail=body.model_dump()) from exc

    return BatchResult(
        received=outcome.received,
        created=outcome.created,
        unchanged=outcome.unchanged,
        stations=outcome.stations,
        dates=outcome.dates,
    )


@router.get(
    "/api/v1/stations/{station_id}/daily",
    response_model=DailyReport,
    summary="查询站点某北京日期的 PM2.5 日报",
)
def get_daily_report(
    station_id: str,
    date: dt.date = Query(..., description="北京时间观测日期 YYYY-MM-DD"),
    service: ReadingService = Depends(get_service),
) -> DailyReport:
    station_id = station_id.strip()
    if not station_id:
        raise HTTPException(status_code=422, detail="station_id must not be blank")
    return service.daily_report(station_id, date)
