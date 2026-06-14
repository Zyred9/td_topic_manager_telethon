"""统一发消息出口。

所有发言(关键字/AI应答/AI自驱/定时)都走这里,共享 phone 全局配额。
- 同号最小间隔:不足则自驱/定时等待,关键字/应答即时性高也短暂等待
- 每分钟配额耗尽:按 source 优先级决定丢弃(关键字>应答)或跳过下轮(自驱>定时)
- FloodWaitError:自动等待 +1 秒重试一次
- 死号类异常(AuthKey* / UserDeactivated* / Session*):标记 is_dead=1
  并把 client 从池里移除,运营在「死号列表」里复活或彻底删除
- 所有发送(成功/失败/配额满/未就绪)都写入 t_send_log,前端按号可查

返回 (ok, reason)。ok=False 时 reason 说明丢弃/失败原因。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from telethon import errors

from config.constants import SendSource
from core.client_manager import client_manager
from core.throttle import throttle
from helpers import td_error
from helpers.dead_account import is_dead_error
from infra.db import run_db
from repositories import account_repo, send_log_repo

logger = logging.getLogger(__name__)

# 配额耗尽时:即时类丢弃(过期无意义),周期类跳过下轮补偿
_DROP_WHEN_FULL = {SendSource.KEYWORD, SendSource.AI_REPLY}


async def _log_send(
    phone: str, chat_id: int, source: SendSource, ok: bool,
    err_code: Optional[str] = None, err_message: Optional[str] = None,
    content_preview: Optional[str] = None,
) -> None:
    """写发送日志。失败只 warning,不抛,绝不阻塞发送主路径。"""
    try:
        await run_db(
            send_log_repo.insert,
            phone, chat_id, source.name, ok,
            err_code, err_message, content_preview,
        )
    except Exception as exc:
        logger.warning("[发送日志] 落库失败 phone=%s chat=%s: %s", phone, chat_id, exc)


async def _mark_dead_and_remove(phone: str, exc: BaseException) -> None:
    """死号类异常统一处理:标 is_dead=1 + 移出 client 池。失败仅记日志。"""
    err_code = type(exc).__name__
    try:
        affected = await run_db(account_repo.mark_dead, phone, err_code, str(exc))
        if affected:
            logger.warning("[死号] phone=%s 已标记失效 reason=%s", phone, err_code)
    except Exception as e2:
        logger.warning("[死号] 标记失败 phone=%s: %s", phone, e2)
    try:
        await client_manager.remove(phone)
    except Exception as e2:
        logger.warning("[死号] 移出 client 池失败 phone=%s: %s", phone, e2)


async def send_text(
    phone: str,
    chat_id: int,
    text: str,
    reply_to: Optional[int] = None,
    source: SendSource = SendSource.AI_SELF,
) -> Tuple[bool, str]:
    preview = (text or "")[:200]

    client = client_manager.get_ready_client(phone)
    if client is None:
        await _log_send(phone, chat_id, source, ok=False,
                        err_code="NOT_READY", err_message="小号未就绪", content_preview=preview)
        return False, "小号未就绪"

    # 每分钟配额
    if not throttle.minute_quota_available(phone):
        action = "丢弃" if source in _DROP_WHEN_FULL else "跳过本轮"
        logger.info("[配额] phone=%s 本分钟已达上限,%s source=%s", phone, action, source.name)
        await _log_send(phone, chat_id, source, ok=False,
                        err_code="QUOTA_FULL", err_message=f"本分钟配额已满({action})",
                        content_preview=preview)
        return False, "本分钟发言配额已满"

    # 最小间隔:等待补齐(单进程协作,sleep 不阻塞其他任务)
    wait = throttle.seconds_until_can_send(phone)
    if wait > 0:
        await asyncio.sleep(wait)

    try:
        await client.send_message(chat_id, text, reply_to=reply_to)
        throttle.mark_sent(phone)
        throttle.incr_minute(phone)
        await _log_send(phone, chat_id, source, ok=True, content_preview=preview)
        return True, ""
    except errors.FloodWaitError as exc:
        logger.warning("[发送] phone=%s chat=%s FLOOD_WAIT %ss,退避重试", phone, chat_id, exc.seconds)
        # 第一次 FloodWait 也记一条,便于事后分析
        await _log_send(phone, chat_id, source, ok=False,
                        err_code="FloodWaitError",
                        err_message=f"FLOOD_WAIT {exc.seconds}s, will retry",
                        content_preview=preview)
        await asyncio.sleep(exc.seconds + 1)
        try:
            await client.send_message(chat_id, text, reply_to=reply_to)
            throttle.mark_sent(phone)
            throttle.incr_minute(phone)
            await _log_send(phone, chat_id, source, ok=True, content_preview=preview)
            return True, ""
        except Exception as exc2:
            await _log_send(phone, chat_id, source, ok=False,
                            err_code=type(exc2).__name__, err_message=td_error.translate(exc2),
                            content_preview=preview)
            if is_dead_error(exc2):
                logger.error("[发送] phone=%s chat=%s 退避后死号异常: %s", phone, chat_id, exc2)
                await _mark_dead_and_remove(phone, exc2)
                return False, f"账号已失效({type(exc2).__name__})"
            logger.error("[发送] phone=%s chat=%s 退避后仍失败: %s", phone, chat_id, exc2)
            return False, td_error.translate(exc2)
    except Exception as exc:
        await _log_send(phone, chat_id, source, ok=False,
                        err_code=type(exc).__name__, err_message=td_error.translate(exc),
                        content_preview=preview)
        if is_dead_error(exc):
            logger.error("[发送] phone=%s chat=%s 死号异常: %s", phone, chat_id, exc)
            await _mark_dead_and_remove(phone, exc)
            return False, f"账号已失效({type(exc).__name__})"
        logger.error("[发送] phone=%s chat=%s 失败: %s", phone, chat_id, exc)
        return False, td_error.translate(exc)
