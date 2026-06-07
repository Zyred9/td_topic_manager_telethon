"""批量群操作 /group-op。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import GroupOpReq
from core.batch_store import batch_store
from core.lifecycle import task_tracker
from services import group_op_service

router = APIRouter(prefix="/group-op", tags=["group-op"])


@router.post("/join")
async def join(req: GroupOpReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    if not req.groupLink:
        raise BizError("缺少群链接")
    batch_id = batch_store.create("join", len(req.phones))
    task_tracker.create_task(group_op_service.run_join(req.phones, req.groupLink, batch_id))
    return result(batch_id)


@router.post("/leave")
async def leave(req: GroupOpReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    if req.chatId is None:
        raise BizError("缺少群 ID")
    batch_id = batch_store.create("leave", len(req.phones))
    task_tracker.create_task(group_op_service.run_leave(req.phones, req.chatId, batch_id))
    return result(batch_id)


@router.get("/result/{batch_id}")
async def get_result(batch_id: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    data = batch_store.get(batch_id)
    if data is None:
        raise BizError("批次不存在或已过期", code=404)
    return result(data)


@router.post("/check-member")
async def check_member(req: GroupOpReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    if req.chatId is None:
        raise BizError("缺少群 ID")
    data = await group_op_service.check_member(req.phones, req.chatId)
    return result(data)
