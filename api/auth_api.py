"""后台登录 /auth。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.deps import BizError, CurrentUser, get_current_user, result
from api.schemas import LoginReq, PasswordChangeReq
from services import auth_service
from services.auth_service import AuthError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(req: LoginReq) -> dict:
    try:
        data = await auth_service.login(req.username, req.password)
    except AuthError as exc:
        raise BizError(str(exc), code=401) from exc
    return result(data)


@router.post("/logout")
async def logout(_: CurrentUser = Depends(get_current_user)) -> dict:
    # JWT 无状态,前端清 token 即可;此处仅记录
    return result()


@router.get("/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    data = await auth_service.get_me(user.user_id)
    if data is None:
        raise BizError("用户不存在", code=401)
    return result(data)


@router.put("/password")
async def change_password(
    req: PasswordChangeReq,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        await auth_service.change_own_password(user.user_id, req.oldPwd, req.newPwd)
    except AuthError as exc:
        raise BizError(str(exc)) from exc
    return result()
