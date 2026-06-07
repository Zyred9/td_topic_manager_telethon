"""定时发送 /schedule。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import (
    ScheduleBatchIdsReq,
    ScheduleBatchStartReq,
    ScheduleBatchStopReq,
    ScheduleStartReq,
)
from services import schedule_service

router = APIRouter(prefix="/schedule", tags=["schedule"])


# 批量路由放在 /{phone} 之前,避免 "batch" 被当作 phone 路径参数匹配
@router.post("/batch/start")
async def batch_start_schedule(
    req: ScheduleBatchStartReq, _: CurrentUser = Depends(get_current_user),
) -> dict:
    """对勾选小号统一下发一份定时配置并启动。"""
    if not req.phones:
        raise BizError("请先勾选小号")
    data = await schedule_service.batch_start(req.phones, req.chatIds, req.content, req.intervalMin)
    return result(data)


@router.post("/batch/stop")
async def batch_stop_schedule(
    req: ScheduleBatchStopReq, _: CurrentUser = Depends(get_current_user),
) -> dict:
    """批量停止勾选小号的定时任务。"""
    if not req.phones:
        raise BizError("请先勾选小号")
    data = await schedule_service.batch_stop(req.phones)
    return result(data)


# ===== 定时任务管理页:列表 / 批量删除 / 批量重启 =====
# 同样放在 /{phone} 之前,避免 "" 与 "batch" 段被当作 phone 匹配


@router.get("")
async def list_schedule_tasks(_: CurrentUser = Depends(get_current_user)) -> dict:
    """全部定时任务列表。"""
    return result(await schedule_service.list_all())


@router.post("/batch/delete")
async def batch_delete_schedule(
    req: ScheduleBatchIdsReq, _: CurrentUser = Depends(get_current_user),
) -> dict:
    """按 id 批量删除定时任务(先停协程再删 DB)。"""
    if not req.ids:
        raise BizError("请先勾选任务")
    return result(await schedule_service.batch_delete(req.ids))


@router.post("/batch/restart")
async def batch_restart_schedule(
    req: ScheduleBatchIdsReq, _: CurrentUser = Depends(get_current_user),
) -> dict:
    """按 id 批量重启定时任务(用表里原配置)。"""
    if not req.ids:
        raise BizError("请先勾选任务")
    return result(await schedule_service.batch_restart(req.ids))


@router.get("/{phone}")
async def get_schedule(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    return result(await schedule_service.get_config(phone))


@router.post("/{phone}/start")
async def start_schedule(
    phone: str, req: ScheduleStartReq, _: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        await schedule_service.start(phone, req.chatIds, req.content, req.intervalMin)
    except ValueError as exc:
        raise BizError(str(exc)) from exc
    return result()


@router.post("/{phone}/stop")
async def stop_schedule(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    await schedule_service.stop(phone)
    return result()
