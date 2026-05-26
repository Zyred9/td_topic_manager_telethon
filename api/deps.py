"""API 公共依赖:统一响应体、JWT 鉴权、超管校验、异常处理。

前端 request.ts 强约束:响应统一 ``{code, message, data}``,
code=200 成功、401 登录过期、403 无权限。HTTP 状态码恒为 200,
业务码放在 body.code,以匹配前端拦截器逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.constants import AdminRole
from services.auth_service import AuthError, decode_token


# ========== 统一响应 ==========

def result(data: Any = None, code: int = 200, message: str = "success") -> dict:
    return {"code": code, "message": message, "data": data}


def page_result(records: list, total: int, page: int, size: int) -> dict:
    """对齐前端 ApiPage<T> = {page, size, total, records}。"""
    return result({"page": page, "size": size, "total": total, "records": records})


class BizError(Exception):
    """业务异常,统一转 {code, message}。默认业务码 500。"""

    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ========== 当前登录用户 ==========

@dataclass
class CurrentUser:
    user_id: int
    username: str
    role: int

    @property
    def is_super_admin(self) -> bool:
        return self.role == int(AdminRole.SUPER_ADMIN)


_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> CurrentUser:
    """解析 Bearer token 得到当前用户;失败抛 401(业务码)。"""
    if credentials is None or not credentials.credentials:
        raise BizError("未登录", code=401)
    try:
        payload = decode_token(credentials.credentials)
    except AuthError as exc:
        raise BizError(str(exc), code=401) from exc
    return CurrentUser(
        user_id=int(payload["sub"]),
        username=payload.get("username", ""),
        role=int(payload.get("role", 0)),
    )


async def require_super_admin(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """/admin/* 二次校验:非超级管理员抛 403。"""
    if not user.is_super_admin:
        raise BizError("无权限,仅超级管理员可访问", code=403)
    return user
