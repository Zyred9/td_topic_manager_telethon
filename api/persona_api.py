"""AI 人设 /persona。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import PersonaModel, PersonaPresetReq, PersonaPreviewReq
from services import persona_service
from services.persona_service import PersonaError

router = APIRouter(prefix="/persona", tags=["persona"])


@router.get("/preset/list")
async def list_presets(_: CurrentUser = Depends(get_current_user)) -> dict:
    return result(await persona_service.list_presets())


@router.post("/preset")
async def add_preset(req: PersonaPresetReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        pid = await persona_service.add_preset(req.presetName, req.preset.model_dump(exclude_none=False))
    except PersonaError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result(pid)


@router.delete("/preset/{preset_id}")
async def delete_preset(preset_id: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await persona_service.delete_preset(preset_id)
    except PersonaError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.get("/{phone}")
async def get_persona(phone: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    return result(await persona_service.get_persona(phone))


@router.put("/{phone}")
async def save_persona(phone: str, req: PersonaModel, _: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        await persona_service.save_persona(phone, req.model_dump(exclude_none=False))
    except PersonaError as exc:
        raise BizError(exc.message, code=exc.code) from exc
    return result()


@router.post("/{phone}/preview")
async def preview(phone: str, req: PersonaPreviewReq, _: CurrentUser = Depends(get_current_user)) -> dict:
    text = await persona_service.preview(phone, None, req.topicPrompt)
    return result(text)
