"""定时发送 /schedule。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import ScheduleStartReq
from services import schedule_service

router = APIRouter(prefix="/schedule", tags=["schedule"])


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
