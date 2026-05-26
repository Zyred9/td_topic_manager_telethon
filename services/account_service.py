"""小号管理:列表、手机号登录、资料编辑、头像、删除级联。

登录流程(手机号):
  login  → 全局凭证建 client,connect,send_code_request,status=2,client 暂存待 code
  code   → sign_in(code);触发 2FA 则 status=6;成功则入池 status=3
  password → sign_in(password=2FA),成功入池

协议号登录在 import_service。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient, errors

from config.constants import AccountStatus, account_status_desc
from config.settings import get_settings
from core import message_sender  # noqa: F401  (确保 core 初始化顺序)
from core.client_manager import client_manager
from core.throttle import throttle
from infra import telethon_factory
from infra.db import get_connection, run_db
from repositories import account_repo, account_watch_repo

logger = logging.getLogger(__name__)


class AccountError(Exception):
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# 登录中暂存:phone -> (client, phone_code_hash, two_fa_password)
_pending_login: Dict[str, Tuple[TelegramClient, str, Optional[str]]] = {}


def _to_vo(acc) -> dict:
    return {
        "id": acc.id,
        "phone": acc.phone,
        "tgFirstName": acc.tg_first_name,
        "tgUsername": acc.tg_username,
        "status": acc.status,
        "statusDesc": account_status_desc(acc.status),
        "bio": acc.bio,
        "loginTime": acc.login_time.strftime("%Y-%m-%d %H:%M:%S") if acc.login_time else None,
    }


async def list_accounts(page_no: int, size: int, keyword: Optional[str], status: Optional[int]) -> dict:
    records, total = await run_db(account_repo.page, page_no, size, keyword, status)
    return {"page": page_no, "size": size, "total": total, "records": [_to_vo(a) for a in records]}


async def list_groups(phone: str, page_no: int, size: int) -> dict:
    """分页查询某小号已加入的群(来自 t_account_watch 平台记录)。"""
    phone = _normalize(phone)
    rows, total = await run_db(account_watch_repo.page_by_phone, phone, page_no, size)
    records = [
        {
            "chatId": r["chat_id"],
            "chatTitle": r.get("chat_title") or str(r["chat_id"]),  # 无群名兜底显示 ID
            "joinedAt": r["joined_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("joined_at") else None,
        }
        for r in rows
    ]
    return {"page": page_no, "size": size, "total": total, "records": records}


async def leave_group(phone: str, chat_id: int) -> None:
    """某小号退出指定群(同步)。"""
    from services import group_op_service
    phone = _normalize(phone)
    try:
        await group_op_service.leave_one(phone, chat_id)
    except Exception as exc:
        raise AccountError(str(exc)) from exc


# ---------- 手机号登录 ----------
async def phone_login(phone: str, email: Optional[str], password: Optional[str]) -> None:
    phone = _normalize(phone)
    settings = get_settings()
    session_path = str(settings.sessions_dir / f"{phone}.session")

    # 已有就绪 client 则先清掉旧的登录态
    old = _pending_login.pop(phone, None)
    if old is not None:
        try:
            await old[0].disconnect()
        except Exception:
            pass

    client = telethon_factory.build_client_for_phone(session_path)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except errors.FloodWaitError as exc:
        await client.disconnect()
        raise AccountError(f"操作频率过高,请等待 {exc.seconds} 秒") from exc
    except Exception as exc:
        await client.disconnect()
        raise AccountError(f"发送验证码失败: {exc}") from exc

    _pending_login[phone] = (client, sent.phone_code_hash, password)
    await run_db(account_repo.upsert_phone, phone, session_path)
    await run_db(account_repo.update_status, phone, int(AccountStatus.WAIT_CODE))
    logger.info("手机号登录已发送验证码 phone=%s", phone)


async def submit_code(phone: str, code: str) -> None:
    phone = _normalize(phone)
    pending = _pending_login.get(phone)
    if pending is None:
        raise AccountError("登录会话已失效,请重新发起登录")
    client, code_hash, two_fa = pending
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
    except errors.SessionPasswordNeededError:
        # 需要 2FA;若登录时带了 password 自动补,否则等前端补提交
        if two_fa:
            await _sign_in_2fa(phone, two_fa)
            return
        await run_db(account_repo.update_status, phone, int(AccountStatus.WAIT_2FA))
        raise AccountError("该账号开启了二级密码,请提交 2FA 密码", code=200)
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as exc:
        raise AccountError("验证码错误或已过期") from exc
    await _finish_login(phone, client)


async def submit_2fa(phone: str, password: str) -> None:
    phone = _normalize(phone)
    if phone not in _pending_login:
        raise AccountError("登录会话已失效,请重新发起登录")
    await _sign_in_2fa(phone, password)


async def _sign_in_2fa(phone: str, password: str) -> None:
    client = _pending_login[phone][0]
    try:
        await client.sign_in(password=password)
    except errors.PasswordHashInvalidError as exc:
        raise AccountError("二级密码错误") from exc
    await _finish_login(phone, client)


async def _finish_login(phone: str, client: TelegramClient) -> None:
    me = await client.get_me()
    await client_manager.register_ready_client(phone, client)
    await run_db(
        account_repo.mark_logged_in,
        phone,
        getattr(me, "id", None),
        getattr(me, "first_name", None),
        getattr(me, "last_name", None),
        getattr(me, "username", None),
    )
    _pending_login.pop(phone, None)
    logger.info("手机号登录成功 phone=%s tg_user_id=%s", phone, getattr(me, "id", None))


# ---------- 资料 / 头像 ----------
async def update_profile(phone: str, first_name, last_name, username, bio) -> None:
    phone = _normalize(phone)
    client = client_manager.get_ready_client(phone)
    if client is None:
        raise AccountError("小号未就绪")
    from telethon.tl import functions
    try:
        if first_name is not None or last_name is not None or bio is not None:
            await client(functions.account.UpdateProfileRequest(
                first_name=first_name, last_name=last_name, about=bio,
            ))
        if username is not None:
            await client(functions.account.UpdateUsernameRequest(username=username))
    except errors.FloodWaitError as exc:
        raise AccountError(f"操作频率过高,请等待 {exc.seconds} 秒") from exc
    except Exception as exc:
        raise AccountError(f"修改资料失败: {exc}") from exc
    await run_db(account_repo.update_profile, phone, first_name, last_name, username, bio)
    logger.info("修改资料 phone=%s", phone)


async def update_avatar(phone: str, file_bytes: bytes, filename: str) -> None:
    phone = _normalize(phone)
    client = client_manager.get_ready_client(phone)
    if client is None:
        raise AccountError("小号未就绪")

    settings = get_settings()
    suffix = (filename.rsplit(".", 1)[-1] if "." in filename else "jpg").lower()
    rel = f"{phone}_{int(datetime.now().timestamp())}.{suffix}"
    abs_path = settings.avatar_dir / rel
    abs_path.write_bytes(file_bytes)

    from telethon.tl import functions
    try:
        uploaded = await client.upload_file(str(abs_path))
        await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
    except Exception as exc:
        raise AccountError(f"上传头像失败: {exc}") from exc
    await run_db(account_repo.update_avatar, phone, rel)
    logger.info("更新头像 phone=%s file=%s", phone, rel)


async def offline(phone: str) -> None:
    phone = _normalize(phone)
    await client_manager.remove(phone)
    await run_db(account_repo.update_status, phone, int(AccountStatus.OFFLINE))
    logger.info("小号主动下线 phone=%s", phone)


# ---------- 删除级联 ----------
async def delete_account(phone: str) -> None:
    phone = _normalize(phone)

    # 1. owner 校验
    topic_names = await run_db(_owner_topic_names, phone)
    if topic_names:
        raise AccountError(
            f"该号是话题 [{', '.join(topic_names)}] 的群主,请先在话题编辑页移交群主再删除",
            code=409,
        )

    # 2. 停内存任务(M3/M4 接入:schedule/topic 停摆)
    try:
        from services import schedule_service
        await schedule_service.stop(phone, persist=False)
    except Exception:
        pass
    try:
        from services import topic_scheduler
        topic_scheduler.remove_member_phone(phone)
    except Exception:
        pass

    # 3. 单事务级联删 DB
    await run_db(_cascade_delete, phone)

    # 4. 断开 client
    await client_manager.remove(phone)

    # 5. 删 session 文件
    try:
        session_file = get_settings().sessions_dir / f"{phone}.session"
        if session_file.exists():
            session_file.unlink()
    except Exception as exc:
        logger.warning("删除 session 文件失败 phone=%s: %s", phone, exc)

    # 6. 清节流内存
    throttle.clear_phone(phone)
    _pending_login.pop(phone, None)
    logger.info("删除小号完成 phone=%s", phone)


def _owner_topic_names(phone: str) -> List[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT topic_name FROM t_ai_topic WHERE owner_phone=%s", (phone,))
            rows = cur.fetchall()
    return [r["topic_name"] for r in rows]


def _cascade_delete(phone: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_schedule_task WHERE phone=%s", (phone,))
            cur.execute("DELETE FROM t_keyword_reply WHERE phone=%s", (phone,))
            cur.execute("DELETE FROM t_ai_topic_member WHERE phone=%s", (phone,))
            cur.execute("DELETE FROM t_account_watch WHERE phone=%s", (phone,))
            cur.execute("DELETE FROM t_account WHERE phone=%s", (phone,))


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"
