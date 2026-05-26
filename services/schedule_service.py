"""定时发送(asyncio 周期任务,一号一任务)。

每号一个后台协程,按 interval_min 周期向多个群发文本;群间额外随机 2~3s 抖动。
发送走 message_sender(source=SCHEDULE,配额最低优先级)。
服务重启不自动恢复:启动时 stop_all_on_startup 全置停。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Dict, List, Optional

from config.constants import SendSource, TaskStatus
from core import message_sender
from infra.db import run_db
from repositories import schedule_repo

logger = logging.getLogger(__name__)

# phone -> 运行中的周期任务
_tasks: Dict[str, asyncio.Task] = {}


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"


async def get_config(phone: str) -> Optional[dict]:
    """返回前端 ScheduleTask|null。"""
    phone = _normalize(phone)
    row = await run_db(schedule_repo.find_by_phone, phone)
    if row is None:
        return None
    return {
        "id": row["id"],
        "phone": row["phone"],
        "chatIds": row["chat_ids"],
        "content": row["content"],
        "intervalMin": row["interval_min"],
        "status": row["status"],
        "lastSent": row["last_sent"].strftime("%Y-%m-%d %H:%M:%S") if row.get("last_sent") else None,
    }


async def start(phone: str, chat_ids: List[int], content: str, interval_min: int) -> None:
    phone = _normalize(phone)
    if interval_min <= 0:
        raise ValueError("发送间隔必须大于 0 分钟")
    if not chat_ids:
        raise ValueError("至少选择一个群")

    chat_ids_str = ",".join(str(c) for c in chat_ids)
    await run_db(schedule_repo.upsert_start, phone, chat_ids_str, content, interval_min)

    # 替换旧任务
    await _cancel_task(phone)
    _tasks[phone] = asyncio.create_task(_run_loop(phone, chat_ids, content, interval_min))
    logger.info("定时发送启动 phone=%s 群数=%d 间隔=%d分钟", phone, len(chat_ids), interval_min)


async def stop(phone: str, persist: bool = True) -> None:
    phone = _normalize(phone)
    await _cancel_task(phone)
    if persist:
        await run_db(schedule_repo.update_status, phone, int(TaskStatus.STOPPED))
    logger.info("定时发送停止 phone=%s", phone)


async def _cancel_task(phone: str) -> None:
    task = _tasks.pop(phone, None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _run_loop(phone: str, chat_ids: List[int], content: str, interval_min: int) -> None:
    interval_sec = interval_min * 60
    try:
        while True:
            for chat_id in chat_ids:
                await message_sender.send_text(phone, chat_id, content, source=SendSource.SCHEDULE)
                await asyncio.sleep(random.uniform(2, 3))  # 群间抖动
            await run_db(schedule_repo.mark_sent, phone)
            await asyncio.sleep(interval_sec)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("定时发送循环异常 phone=%s", phone)


async def stop_all_on_startup() -> None:
    """服务启动:DB 全置停(不自动恢复任务)。"""
    await run_db(schedule_repo.stop_all_on_startup)
