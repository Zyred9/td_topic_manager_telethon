"""账号管理 /admin/user(仅超级管理员)。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, require_super_admin, result
from api.schemas import AdminCreateReq, AdminResetPwdReq, AdminUpdateReq
from services import admin_service
from services.admin_service import AdminError

router = APIRouter(prefix="/admin/user", tags=["admin"])


@router.get("/list")
async def list_users(_: CurrentUser = Depends(require_super_admin)) -> dict:
    return result(await admin_service.list_users())


@router.post("")
async def create_user(
    req: AdminCreateReq,
    _: CurrentUser = Depends(require_super_admin),
) -> dict:
    try:
        user_id = await admin_service.create_user(req.username, req.password, req.role)
    except AdminError as exc:
        raise BizError(str(exc)) from exc
    return result(user_id)


@router.put("/{user_id}")
async def update_user(
    user_id: int,
    req: AdminUpdateReq,
    _: CurrentUser = Depends(require_super_admin),
) -> dict:
    try:
        await admin_service.update_user(user_id, req.role, req.enabled)
    except AdminError as exc:
        raise BizError(str(exc)) from exc
    return result()


@router.put("/{user_id}/password")
async def reset_password(
    user_id: int,
    req: AdminResetPwdReq,
    _: CurrentUser = Depends(require_super_admin),
) -> dict:
    try:
        await admin_service.reset_password(user_id, req.newPwd)
    except AdminError as exc:
        raise BizError(str(exc)) from exc
    return result()


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    current: CurrentUser = Depends(require_super_admin),
) -> dict:
    try:
        await admin_service.delete_user(user_id, current.user_id)
    except AdminError as exc:
        raise BizError(str(exc)) from exc
    return result()
