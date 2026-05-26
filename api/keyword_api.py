"""关键字回复 /keyword。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import KeywordReq
from services import keyword_service

router = APIRouter(prefix="/keyword", tags=["keyword"])


@router.get("/{phone}")
async def list_keywords(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    return result(await keyword_service.list_keywords(phone))


@router.post("/{phone}")
async def add_keyword(phone: str, req: KeywordReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await keyword_service.add_keyword(phone, req.keyword, req.replyText)
    except ValueError as exc:
        raise BizError(str(exc)) from exc
    return result()


@router.delete("/{phone}/{kw_id}")
async def delete_keyword(phone: str, kw_id: int, _: CurrentUser = Depends(get_current_user)) -> dict:
    await keyword_service.delete_keyword(phone, kw_id)
    return result()
