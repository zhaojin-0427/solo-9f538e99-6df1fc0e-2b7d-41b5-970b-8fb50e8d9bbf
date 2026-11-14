"""FastAPI 应用工厂与入口。"""

from __future__ import annotations

import logging
import math
import sqlite3
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import router
from .config import Settings
from .db import Database
from .service import ReadingService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pm25")


def _json_safe(value: object) -> object:
    """把 NaN/Infinity 等不合法 JSON 值转换成字符串，保证错误响应可序列化。

    例如提交 ``1e400`` 会在 Python 中解析为 inf，pydantic 拒绝它，
    但错误详情回显该输入时会让 json.dumps 失败。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str):
        return value if value.isprintable() else value.encode("unicode_escape").decode("ascii")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 在启动时建表，避免仅仅 import 模块就产生数据库文件。
        database.initialize()
        logger.info("database ready at %s", settings.database_path)
        yield

    app = FastAPI(
        title="PM2.5 站点日报 API",
        version=__version__,
        lifespan=lifespan,
        description=(
            "接收站点 PM2.5 小时值补报、校准修订与撤回，并按北京时间日期查询日均值。\n\n"
            "修订语义：每小时以最大 revision 为准；同号同内容幂等；同号不同内容整批 409。"
        ),
    )
    app.state.database = database
    app.state.service = ReadingService(database)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "invalid request",
                "errors": _json_safe(jsonable_encoder(exc.errors())),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # HTTPException.detail 已是 dict 时原样返回，保持 ConflictResponse 结构。
        content = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(sqlite3.OperationalError)
    async def _sqlite_busy_handler(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        # 事务在异常时已回滚；这里只负责把「库忙」转成可重试的 503。
        logger.warning("sqlite operational error on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=503,
            content={
                "code": "database_busy",
                "message": "database temporarily busy; batch was not applied, retry later",
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "request failed, no partial writes were committed"},
        )

    app.include_router(router)
    return app


app = create_app()
