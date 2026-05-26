"""私聊记录 /dm。三层:小号 → 私聊对象 → 消息。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import CurrentUser, get_current_user, result
from services import dm_service

router = APIRouter(prefix="/dm", tags=["dm"])


@router.get("/accounts")
async def list_accounts(_: CurrentUser = Depends(get_current_user)) -> dict:
    """有私聊记录的小号列表(第一层)。"""
    return result(await dm_service.list_accounts_with_dm())


@router.get("/{phone}/peers")
async def list_peers(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    """某小号的私聊对象列表(第二层)。"""
    return result(await dm_service.list_peers(phone))


@router.get("/{phone}/messages")
async def list_messages(
    phone: str, peerUserId: int, page: int = 1, size: int = 20,
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    """某小号与某对象的消息分页(第三层)。"""
    return result(await dm_service.list_messages(phone, peerUserId, page, size))
