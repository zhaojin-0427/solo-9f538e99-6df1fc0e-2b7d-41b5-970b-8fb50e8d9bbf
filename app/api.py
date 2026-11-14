"""HTTP 接口层。"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .event_models import (
    ConfirmationListResponse,
    ConfirmationRequest,
    EventQuery,
    EventQueryRequest,
    EventQueryResponse,
)
from .events import (
    EventService,
    InvalidEventReferenceError,
    StaleConfirmationError,
)
from .models import BatchRequest, BatchResult, ConflictResponse, DailyReport
from .service import ReadingService, RevisionConflictError

logger = logging.getLogger(__name__)

router = APIRouter()


def get_service(request: Request) -> ReadingService:
    return request.app.state.service


def get_event_service(request: Request) -> EventService:
    return request.app.state.event_service


def _clean_station_id(station_id: str) -> str:
    station_id = station_id.strip()
    if not station_id:
        raise HTTPException(status_code=422, detail="station_id must not be blank")
    return station_id


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
    station_id = _clean_station_id(station_id)
    return service.daily_report(station_id, date)


# ---------------------------------------------------------------- 事件查询
def _event_query_response(
    service: EventService,
    station_id: str,
    start_date: dt.date,
    end_date: dt.date,
    threshold: float,
) -> EventQueryResponse:
    events, links = service.list_events(station_id, start_date, end_date, threshold)
    return EventQueryResponse(
        station_id=station_id,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        threshold=threshold,
        event_count=len(events),
        events=[event.as_dict(links.get(event.event_id)) for event in events],
    )


def _resolve_station(station_id: str | None) -> str:
    station = (station_id or "").strip()
    if not station:
        raise HTTPException(status_code=422, detail="station_id is required")
    return station


@router.get(
    "/api/v1/stations/{station_id}/events",
    response_model=EventQueryResponse,
    summary="查询站点连续超阈值事件",
)
def get_station_events(
    station_id: str,
    query: EventQuery = Query(...),
    service: EventService = Depends(get_event_service),
) -> EventQueryResponse:
    """查询北京时间日期区间（含两端，最多 3 天）内的连续超阈值事件。

    * 只使用每小时最高修订号的有效浓度；
    * 严格大于 ``threshold``，连续至少 3 小时才构成事件；
    * 缺测或撤回截断事件；跨午夜仍连续。

    等价入口：``GET /api/v1/events?station_id=…``、
    ``POST /api/v1/stations/{station_id}/events/query``。
    """
    return _event_query_response(
        service,
        _clean_station_id(station_id),
        query.start_date,
        query.end_date,
        query.threshold,
    )


@router.get(
    "/api/v1/events",
    response_model=EventQueryResponse,
    summary="查询连续超阈值事件（站点作为查询参数）",
)
def get_events(
    station_id: str = Query(..., description="站点 ID"),
    query: EventQuery = Query(...),
    service: EventService = Depends(get_event_service),
) -> EventQueryResponse:
    return _event_query_response(
        service,
        _clean_station_id(station_id),
        query.start_date,
        query.end_date,
        query.threshold,
    )


@router.post(
    "/api/v1/stations/{station_id}/events/query",
    response_model=EventQueryResponse,
    summary="查询连续超阈值事件（POST 形式）",
)
def post_station_events(
    station_id: str,
    payload: EventQueryRequest,
    service: EventService = Depends(get_event_service),
) -> EventQueryResponse:
    return _event_query_response(
        service,
        _clean_station_id(station_id),
        payload.start_date,
        payload.end_date,
        payload.threshold,
    )


@router.post(
    "/api/v1/events/query",
    response_model=EventQueryResponse,
    summary="查询连续超阈值事件（POST，站点在请求体中）",
)
def post_events(
    payload: EventQueryRequest,
    service: EventService = Depends(get_event_service),
) -> EventQueryResponse:
    return _event_query_response(
        service,
        _resolve_station(payload.station_id),
        payload.start_date,
        payload.end_date,
        payload.threshold,
    )


@router.get(
    "/api/v1/events/query",
    response_model=EventQueryResponse,
    summary="查询连续超阈值事件（GET，站点在查询参数中）",
)
def get_events_query(
    station_id: str = Query(..., description="站点 ID"),
    query: EventQuery = Query(...),
    service: EventService = Depends(get_event_service),
) -> EventQueryResponse:
    return _event_query_response(
        service,
        _clean_station_id(station_id),
        query.start_date,
        query.end_date,
        query.threshold,
    )


# ---------------------------------------------------------------- 事件确认
def _confirm(
    service: EventService,
    station_id: str,
    payload: ConfirmationRequest,
    path_event_id: str | None = None,
) -> JSONResponse:
    # 事件标识按优先级归一化：显式 event_id > event 对象/event_ids > 起止时刻。
    # event_id 是内容指纹，给出时直接定位事件；否则由 service 用起止时刻匹配。
    event_ids = payload.requested_event_ids()
    if path_event_id:
        event_ids = [path_event_id, *(event_ids or [])]

    event_start, event_end = payload.time_identity()

    try:
        confirmations = service.confirm(
            station_id,
            payload.start_date,
            payload.end_date,
            payload.threshold,
            event_ids=event_ids,
            event_start=event_start,
            event_end=event_end,
            expected_hours=payload.expected_hours(),
        )
    except StaleConfirmationError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "code": "stale_confirmation",
                "message": (
                    "event content or boundaries changed since it was observed; "
                    "re-query events and confirm again. No state was modified."
                ),
                "requested_event_ids": exc.requested_event_ids,
                "available_event_ids": exc.available_event_ids,
            },
        )
    except InvalidEventReferenceError as exc:
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "message": str(exc)},
        )
    except ValueError as exc:  # 事件标识为空等参数问题
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "message": str(exc)},
        )

    body: dict = {
        "station_id": confirmations[0].station_id,
        "start_date": confirmations[0].start_date,
        "end_date": confirmations[0].end_date,
        "threshold": confirmations[0].threshold,
        # 绑定这几项即绑定本次查询；confirmations 是本请求覆盖的事件列表。
        "confirmations": [item.as_dict() for item in confirmations],
    }
    if len(confirmations) == 1:
        # 单事件确认时把该确认的字段同时放在顶层，便于直接消费。
        first = confirmations[0]
        top_level = first.as_dict()
        top_level.pop("confirmations", None)
        body.update(top_level)
    return JSONResponse(status_code=200, content=body)


_CONFIRM_RESPONSES = {
    200: {"description": "确认已记录；重复确认同一事件是幂等的。"},
    409: {
        "description": "事件内容或边界已改变，过期确认被拒绝，且未改动任何现有状态。",
    },
    422: {"description": "请求校验失败或事件定位参数非法。"},
}


@router.post(
    "/api/v1/stations/{station_id}/events/confirm",
    summary="确认本次查询所见的事件",
    responses=_CONFIRM_RESPONSES,
)
def confirm_station_events(
    station_id: str,
    payload: ConfirmationRequest,
    service: EventService = Depends(get_event_service),
) -> JSONResponse:
    """在提交时刻的数据库状态上重新校验事件后记录确认。

    请求必须携带本次查询的站点、区间与阈值，以及所见事件（``event_id``、
    ``event_ids``，或事件起止时刻）。事件已被补报 / 修订 / 撤回改写时返回 409。
    """
    return _confirm(service, _clean_station_id(station_id), payload)


@router.post(
    "/api/v1/events/confirm",
    summary="确认本次查询所见的事件（站点在请求体中）",
    responses=_CONFIRM_RESPONSES,
)
def confirm_events(
    payload: ConfirmationRequest,
    service: EventService = Depends(get_event_service),
) -> JSONResponse:
    return _confirm(service, _resolve_station(payload.station_id), payload)


@router.post(
    "/api/v1/stations/{station_id}/events/{event_id}/confirm",
    summary="确认单个事件（事件 ID 在路径中）",
    responses=_CONFIRM_RESPONSES,
)
def confirm_station_event(
    station_id: str,
    event_id: str,
    payload: ConfirmationRequest,
    service: EventService = Depends(get_event_service),
) -> JSONResponse:
    return _confirm(
        service, _clean_station_id(station_id), payload, path_event_id=event_id
    )


@router.post(
    "/api/v1/events/{event_id}/confirm",
    summary="确认单个事件（站点在请求体中）",
    responses=_CONFIRM_RESPONSES,
)
def confirm_event(
    event_id: str,
    payload: ConfirmationRequest,
    service: EventService = Depends(get_event_service),
) -> JSONResponse:
    return _confirm(
        service,
        _resolve_station(payload.station_id),
        payload,
        path_event_id=event_id,
    )


@router.get(
    "/api/v1/stations/{station_id}/confirmations",
    response_model=ConfirmationListResponse,
    summary="列出站点的历史确认及其当前有效性",
)
def list_station_confirmations(
    station_id: str,
    service: EventService = Depends(get_event_service),
) -> ConfirmationListResponse:
    station_id = _clean_station_id(station_id)
    return ConfirmationListResponse(
        station_id=station_id,
        confirmations=service.list_confirmations(station_id),
    )
