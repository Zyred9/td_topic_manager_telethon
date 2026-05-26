"""后台账号管理(仅超级管理员)。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from config.constants import role_desc
from infra.db import run_db
from repositories import admin_user_repo
from services.auth_service import hash_password

logger = logging.getLogger(__name__)


class AdminError(Exception):
    """账号管理业务异常。"""


def _to_vo(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "roleDesc": role_desc(user.role),
        "enabled": user.enabled,
        "lastLogin": _fmt(user.last_login),
        "createTime": _fmt(user.create_time),
        "updateTime": _fmt(user.update_time),
    }


async def list_users() -> List[dict]:
    users = await run_db(admin_user_repo.list_all)
    return [_to_vo(u) for u in users]


async def create_user(username: str, password: str, role: int) -> int:
    existing = await run_db(admin_user_repo.find_by_username, username)
    if existing is not None:
        raise AdminError("用户名已存在")
    user_id = await run_db(admin_user_repo.insert, username, hash_password(password), role)
    logger.info("新增后台账号 username=%s role=%s", username, role)
    return user_id


async def update_user(user_id: int, role: Optional[int], enabled: Optional[int]) -> None:
    user = await run_db(admin_user_repo.find_by_id, user_id)
    if user is None:
        raise AdminError("账号不存在")
    await run_db(admin_user_repo.update_role_enabled, user_id, role, enabled)
    logger.info("编辑后台账号 id=%s role=%s enabled=%s", user_id, role, enabled)


async def reset_password(user_id: int, new_pwd: str) -> None:
    user = await run_db(admin_user_repo.find_by_id, user_id)
    if user is None:
        raise AdminError("账号不存在")
    await run_db(admin_user_repo.update_password, user_id, hash_password(new_pwd))
    logger.info("重置后台账号密码 id=%s", user_id)


async def delete_user(user_id: int, current_user_id: int) -> None:
    if user_id == current_user_id:
        raise AdminError("不能删除自己")
    user = await run_db(admin_user_repo.find_by_id, user_id)
    if user is None:
        raise AdminError("账号不存在")
    await run_db(admin_user_repo.delete, user_id)
    logger.info("删除后台账号 id=%s", user_id)


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
