"""M8 入口：批次对照排程的 API 与 /allocation 页面。

- `POST /api/allocation/plan`     生成方案（draft，不占位）
- `POST /api/allocation/confirm`  整批确认（全有或全无，顶替上一版）
- `POST /api/allocation/retry`    失败重试（恢复原分配重排，不重复占位）
- `GET  /api/allocation`          只读状态（占用、方案历史、最近结果）
- `GET  /allocation`              页面
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ... import web
from ...errors import PriceliftError
from . import service

router = APIRouter()


class PlanIn(BaseModel):
    experiments: str = Field("", description="实验清单，每行：编号,商品,渠道,开始日,结束日")


class PlanRef(BaseModel):
    plan_id: str


def _batch_id(request: Request) -> str | None:
    ctx = web.get_ctx(request)
    batch = ctx.current_batch(request.query_params.get("batch_id"))
    return batch["batch_id"] if batch else None


def _require_batch(request: Request) -> str:
    batch_id = _batch_id(request)
    if batch_id is None:
        from ...errors import Unprocessable
        raise Unprocessable("还没有批次，先导入再排程", code="S_NO_BATCH")
    return batch_id


@router.post("/api/allocation/plan")
def api_plan(body: PlanIn, request: Request) -> dict:
    ctx = web.get_ctx(request)
    batch_id = _require_batch(request)
    return service.plan(ctx.store, batch_id, body.experiments)


@router.post("/api/allocation/confirm")
def api_confirm(body: PlanRef, request: Request) -> dict:
    ctx = web.get_ctx(request)
    _require_batch(request)
    return service.confirm(ctx.store, body.plan_id)


@router.post("/api/allocation/retry")
def api_retry(body: PlanRef, request: Request) -> dict:
    ctx = web.get_ctx(request)
    batch_id = _require_batch(request)
    return service.retry_plan(ctx.store, body.plan_id)


@router.get("/api/allocation")
def api_state(request: Request) -> dict:
    ctx = web.get_ctx(request)
    batch_id = _batch_id(request)
    if batch_id is None:
        return {"batch_id": None, "occupancy": [], "plans": [],
                "latest_plan": None, "latest_confirmed_at": None}
    return service.state(ctx.store, batch_id)


@router.get("/allocation")
def page_allocation(request: Request):
    ctx = web.get_ctx(request)
    batch_id = _batch_id(request)
    view = None
    error = None
    if batch_id is not None:
        try:
            view = service.state(ctx.store, batch_id)
        except PriceliftError as exc:
            error = {"code": exc.code, "message": exc.message}
    else:
        error = {"code": "S_NO_BATCH", "message": "还没有批次，先导入再排程"}
    return web.render(
        request, "allocation.html",
        view=view,
        error=error,
        sample_text=service.sample_text(),
        stress_text=service.sample_text(stress=True),
    )
