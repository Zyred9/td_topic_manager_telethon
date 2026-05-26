"""后台鉴权:密码 bcrypt 校验、JWT 签发/解析、登录、改密。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from config.constants import AdminRole, role_desc
from config.settings import get_settings
from entities.admin_user import AdminUser
from infra.db import run_db
from repositories import admin_user_repo

logger = logging.getLogger(__name__)

_ALGO = "HS256"


class AuthError(Exception):
    """鉴权失败(用户名密码错、停用、token 无效等)。"""


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def issue_token(user: AdminUser) -> str:
    cfg = get_settings().auth
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=cfg.jwt_expire_min),
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=_ALGO)


def decode_token(token: str) -> dict:
    """解析 JWT,失败抛 AuthError。"""
    cfg = get_settings().auth
    try:
        return jwt.decode(token, cfg.jwt_secret, algorithms=[_ALGO])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("登录已过期,请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("无效的登录凭证") from exc


async def login(username: str, password: str) -> dict:
    """登录,返回前端 LoginResponse 形状。失败抛 AuthError。"""
    user = await run_db(admin_user_repo.find_by_username, username)
    if user is None or not verify_password(password, user.password):
        raise AuthError("用户名或密码错误")
    if user.enabled != 1:
        raise AuthError("账号已停用")

    await run_db(admin_user_repo.update_last_login, user.id)
    token = issue_token(user)
    logger.info("后台登录成功 username=%s role=%s", username, user.role)
    return {
        "token": token,
        "userId": user.id,
        "username": user.username,
        "role": user.role,
        "roleDesc": role_desc(user.role),
    }


async def get_me(user_id: int) -> Optional[dict]:
    user = await run_db(admin_user_repo.find_by_id, user_id)
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "enabled": user.enabled,
        "lastLogin": _fmt(user.last_login),
        "createTime": _fmt(user.create_time),
    }


async def change_own_password(user_id: int, old_pwd: str, new_pwd: str) -> None:
    user = await run_db(admin_user_repo.find_by_id, user_id)
    if user is None:
        raise AuthError("用户不存在")
    if not verify_password(old_pwd, user.password):
        raise AuthError("原密码错误")
    await run_db(admin_user_repo.update_password, user_id, hash_password(new_pwd))
    logger.info("用户 id=%s 修改了自己的密码", user_id)


async def ensure_init_admin() -> None:
    """首次启动:无超级管理员则用 .env 配置创建一个。"""
    count = await run_db(admin_user_repo.count_super_admin)
    if count > 0:
        return
    cfg = get_settings().auth
    await run_db(
        admin_user_repo.insert,
        cfg.init_admin_user,
        hash_password(cfg.init_admin_password),
        int(AdminRole.SUPER_ADMIN),
    )
    logger.warning(
        "已创建默认超级管理员 username=%s,请首次登录后立即修改密码",
        cfg.init_admin_user,
    )


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
