"""统一发消息出口。

所有发言(关键字/AI应答/AI自驱/定时)都走这里,共享 phone 全局配额。
- 同号最小间隔:不足则自驱/定时等待,关键字/应答即时性高也短暂等待
- 每分钟配额耗尽:按 source 优先级决定丢弃(关键字>应答)或跳过下轮(自驱>定时)
- FloodWaitError:自动等待 +1 秒重试一次

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

logger = logging.getLogger(__name__)

# 配额耗尽时:即时类丢弃(过期无意义),周期类跳过下轮补偿
_DROP_WHEN_FULL = {SendSource.KEYWORD, SendSource.AI_REPLY}


async def send_text(
    phone: str,
    chat_id: int,
    text: str,
    reply_to: Optional[int] = None,
    source: SendSource = SendSource.AI_SELF,
) -> Tuple[bool, str]:
    client = client_manager.get_ready_client(phone)
    if client is None:
        return False, "小号未就绪"

    # 每分钟配额
    if not throttle.minute_quota_available(phone):
        action = "丢弃" if source in _DROP_WHEN_FULL else "跳过本轮"
        logger.info("[配额] phone=%s 本分钟已达上限,%s source=%s", phone, action, source.name)
        return False, "本分钟发言配额已满"

    # 最小间隔:等待补齐(单进程协作,sleep 不阻塞其他任务)
    wait = throttle.seconds_until_can_send(phone)
    if wait > 0:
        await asyncio.sleep(wait)

    try:
        await client.send_message(chat_id, text, reply_to=reply_to)
        throttle.mark_sent(phone)
        throttle.incr_minute(phone)
        return True, ""
    except errors.FloodWaitError as exc:
        logger.warning("[发送] phone=%s chat=%s FLOOD_WAIT %ss,退避重试", phone, chat_id, exc.seconds)
        await asyncio.sleep(exc.seconds + 1)
        try:
            await client.send_message(chat_id, text, reply_to=reply_to)
            throttle.mark_sent(phone)
            throttle.incr_minute(phone)
            return True, ""
        except Exception as exc2:
            logger.error("[发送] phone=%s chat=%s 退避后仍失败: %s", phone, chat_id, exc2)
            return False, td_error.translate(exc2)
    except Exception as exc:
        logger.error("[发送] phone=%s chat=%s 失败: %s", phone, chat_id, exc)
        return False, td_error.translate(exc)
