"""AI 话题 /topic。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import TopicCreateReq, TopicUpdateReq, TransferOwnerReq
from services import topic_service
from services.topic_service import TopicError

router = APIRouter(prefix="/topic", tags=["topic"])


@router.get("/list")
async def list_topics(_: CurrentUser = Depends(get_current_user)) -> dict:
    return result(await topic_service.list_topics())


@router.get("/{topic_id}")
async def get_topic(topic_id: int, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        return result(await topic_service.get_topic(topic_id))
    except TopicError as exc:
        raise BizError(exc.message, code=exc.code) from exc


@router.post("")
async def create_topic(req: TopicCreateReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        return result(await topic_service.create_topic(req))
    except TopicError as exc:
        raise BizError(exc.message, code=exc.code) from exc


@router.put("/{topic_id}")
async def update_topic(topic_id: int, req: TopicUpdateReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        return result(await topic_service.update_topic(topic_id, req))
    except TopicError as exc:
        raise BizError(exc.message, code=exc.code) from exc


@router.post("/{topic_id}/start")
async def start_topic(topic_id: int, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await topic_service.start_topic(topic_id)
    except TopicError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/{topic_id}/stop")
async def stop_topic(topic_id: int, _: CurrentUser = Depends(get_current_user)) -> dict:
    await topic_service.stop_topic(topic_id)
    return result()


@router.delete("/{topic_id}")
async def delete_topic(topic_id: int, _: CurrentUser = Depends(get_current_user)) -> dict:
    await topic_service.delete_topic(topic_id)
    return result()


@router.post("/{topic_id}/transfer-owner")
async def transfer_owner(
    topic_id: int, req: TransferOwnerReq, _: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        await topic_service.transfer_owner(topic_id, req.newOwnerPhone, req.password)
    except TopicError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()
