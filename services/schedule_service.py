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
from repositories import account_repo, schedule_repo

logger = logging.getLogger(__name__)

# phone -> 运行中的周期任务
_tasks: Dict[str, asyncio.Task] = {}
# ponytail: 定时任务状态变更是低频操作，一个进程内锁即可保证 DB 与内存任务顺序一致。
_task_state_lock = asyncio.Lock()


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"


def _to_vo(row: dict) -> dict:
    """t_schedule_task 行 → 前端 ScheduleTask VO。"""
    return {
        "id": row["id"],
        "phone": row["phone"],
        "chatIds": row["chat_ids"],
        "content": row["content"],
        "intervalMin": row["interval_min"],
        "intervalSec": row.get("interval_sec", 0),
        "status": row["status"],
        "lastSent": row["last_sent"].strftime("%Y-%m-%d %H:%M:%S") if row.get("last_sent") else None,
    }


async def get_config(phone: str) -> Optional[dict]:
    """返回前端 ScheduleTask|null。"""
    phone = _normalize(phone)
    row = await run_db(schedule_repo.find_by_phone, phone)
    if row is None:
        return None
    return _to_vo(row)


async def list_all() -> List[dict]:
    """全部定时任务列表(给定时任务管理页)。"""
    rows = await run_db(schedule_repo.list_all)
    return [_to_vo(row) for row in rows]


async def start(phone: str, chat_ids: List[int], content: str, interval_min: int, interval_sec: int = 0) -> None:
    phone = _normalize(phone)
    async with _task_state_lock:
        await _start_locked(phone, chat_ids, content, interval_min, interval_sec)


async def _start_locked(phone: str, chat_ids: List[int], content: str, interval_min: int, interval_sec: int) -> None:
    from core.client_manager import client_manager

    if interval_min < 0 or interval_sec < 0:
        raise ValueError("发送间隔不能为负数")
    if interval_min == 0 and interval_sec <= 0:
        raise ValueError("发送间隔必须大于 0 秒")
    if not chat_ids:
        raise ValueError("至少选择一个群")
    if not content or not content.strip():
        raise ValueError("发送内容不能为空")
    if client_manager.get_ready_client(phone) is None:
        raise ValueError("小号未就绪(未登录/离线)")

    chat_ids_str = ",".join(str(c) for c in chat_ids)
    await run_db(schedule_repo.upsert_start, phone, chat_ids_str, content, interval_min, interval_sec)

    # 替换旧任务
    await _cancel_task(phone)
    _tasks[phone] = asyncio.create_task(_run_loop(phone, chat_ids, content, interval_min, interval_sec))
    logger.info("定时发送启动 phone=%s 群数=%d 间隔=%d分%d秒", phone, len(chat_ids), interval_min, interval_sec)


async def stop(phone: str, persist: bool = True) -> None:
    phone = _normalize(phone)
    async with _task_state_lock:
        if persist:
            await run_db(schedule_repo.update_status, phone, int(TaskStatus.STOPPED))
        await _cancel_task(phone)
    logger.info("定时发送停止 phone=%s", phone)


async def _cancel_task(phone: str) -> None:
    task = _tasks.pop(phone, None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _run_loop(phone: str, chat_ids: List[int], content: str, interval_min: int, interval_sec: int = 0) -> None:
    from core.client_manager import client_manager

    interval_total_sec = interval_min * 60 + interval_sec
    try:
        while True:
            try:
                await asyncio.sleep(interval_total_sec)  # 启动后先等一个间隔再发,第 0 秒不发送
                sent_any = False
                for chat_id in chat_ids:
                    ok, reason = await message_sender.send_text(
                        phone, chat_id, content, source=SendSource.SCHEDULE,
                    )
                    if not ok:
                        # 配额满只代表本轮跳过,不能把正常群永久移出定时任务。
                        if reason == message_sender.REASON_QUOTA_FULL:
                            break
                        # client 暂时不在线不等于死号；仅 DB 已判死或账号已删除时永久停止。
                        if client_manager.get_ready_client(phone) is None:
                            account = await run_db(account_repo.find_by_phone, phone)
                            if account is None or int(account.is_dead) == 1:
                                async with _task_state_lock:
                                    if _tasks.get(phone) is asyncio.current_task():
                                        await run_db(
                                            schedule_repo.update_status,
                                            phone,
                                            int(TaskStatus.STOPPED),
                                        )
                                logger.warning("[定时] phone=%s 已判死或删除,定时任务停止:%s", phone, reason)
                                return
                            logger.warning("[定时] phone=%s 临时未就绪,下轮重试:%s", phone, reason)
                            break
                        # 号还在,只是本轮发送失败(Flood/慢速/网络等):记日志跳过,下轮继续,不改群列表
                        logger.warning("[定时] phone=%s 群 %s 本轮发送失败,下轮重试:%s",
                                       phone, chat_id, reason)
                    else:
                        sent_any = True
                    await asyncio.sleep(random.uniform(2, 3))  # 群间抖动
                if sent_any:
                    await run_db(schedule_repo.mark_sent, phone)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[定时] phone=%s 本轮发送异常,跳过本次,下一轮继续", phone)
    except asyncio.CancelledError:
        raise
    finally:
        if _tasks.get(phone) is asyncio.current_task():
            _tasks.pop(phone, None)


# ---------- 批量(统一一份配置下发) ----------
async def batch_start(phones: List[str], chat_ids: List[int], content: str, interval_min: int, interval_sec: int = 0) -> dict:
    """对所有勾选小号下发同一份定时配置并启动。逐号隔离,返回成功/失败列表。

    未就绪(未登录/离线)的号跳过并记入 failed,不阻断其它号。
    """
    ok: List[str] = []
    failed: List[dict] = []
    for raw in phones:
        phone = _normalize(raw)
        try:
            await start(phone, chat_ids, content, interval_min, interval_sec)
            ok.append(phone)
        except ValueError as exc:
            failed.append({"phone": phone, "reason": str(exc)})
        except Exception as exc:
            logger.warning("批量启动定时失败 phone=%s", phone, exc_info=exc)
            failed.append({"phone": phone, "reason": "启动异常"})
    logger.info("批量定时启动 成功=%d 失败=%d", len(ok), len(failed))
    return {"ok": ok, "failed": failed}


async def batch_stop(phones: List[str]) -> dict:
    """批量停止勾选小号的定时任务。逐号隔离,返回成功/失败列表。"""
    ok: List[str] = []
    failed: List[dict] = []
    for raw in phones:
        phone = _normalize(raw)
        try:
            await stop(phone)
            ok.append(phone)
        except Exception as exc:
            logger.warning("批量停止定时失败 phone=%s", phone, exc_info=exc)
            failed.append({"phone": phone, "reason": "停止异常"})
    logger.info("批量定时停止 成功=%d 失败=%d", len(ok), len(failed))
    return {"ok": ok, "failed": failed}


# ---------- 按 id 管理(定时任务管理页) ----------
async def delete(task_id: int) -> None:
    """删除单条定时任务:DB 删除成功后再停运行中的发送协程。"""
    async with _task_state_lock:
        row = await run_db(schedule_repo.find_by_id, task_id)
        if row is None:
            raise ValueError("任务不存在")
        await run_db(schedule_repo.delete_by_id, task_id)
        await _cancel_task(_normalize(row["phone"]))
    logger.info("定时任务删除 id=%s phone=%s", task_id, row["phone"])


async def restart(task_id: int) -> None:
    """用表里存的原配置重启单条定时任务。"""
    async with _task_state_lock:
        row = await run_db(schedule_repo.find_by_id, task_id)
        if row is None:
            raise ValueError("任务不存在")
        phone = _normalize(row["phone"])
        chat_ids = [int(c) for c in str(row["chat_ids"]).split(",") if c.strip()]
        await _start_locked(phone, chat_ids, row["content"], row["interval_min"], row.get("interval_sec", 0))
    logger.info("定时任务重启 id=%s phone=%s", task_id, row["phone"])


async def batch_delete(ids: List[int]) -> dict:
    """批量删除定时任务。逐 id 隔离,返回成功/失败列表。"""
    ok: List[str] = []
    failed: List[dict] = []
    for task_id in ids:
        try:
            await delete(task_id)
            ok.append(str(task_id))
        except ValueError as exc:
            failed.append({"phone": str(task_id), "reason": str(exc)})
        except Exception as exc:
            logger.warning("批量删除定时任务失败 id=%s", task_id, exc_info=exc)
            failed.append({"phone": str(task_id), "reason": "删除异常"})
    logger.info("批量删除定时任务 成功=%d 失败=%d", len(ok), len(failed))
    return {"ok": ok, "failed": failed}


async def batch_restart(ids: List[int]) -> dict:
    """批量重启定时任务(用各自表里的原配置)。逐 id 隔离,返回成功/失败列表。"""
    ok: List[str] = []
    failed: List[dict] = []
    for task_id in ids:
        row = await run_db(schedule_repo.find_by_id, task_id)
        if row is None:
            failed.append({"phone": str(task_id), "reason": "任务不存在"})
            continue
        phone = _normalize(row["phone"])
        try:
            await restart(task_id)
            ok.append(phone)
        except ValueError as exc:
            failed.append({"phone": phone, "reason": str(exc)})
        except Exception as exc:
            logger.warning("批量重启定时任务失败 id=%s", task_id, exc_info=exc)
            failed.append({"phone": phone, "reason": "重启异常"})
    logger.info("批量重启定时任务 成功=%d 失败=%d", len(ok), len(failed))
    return {"ok": ok, "failed": failed}


async def cancel_all_tasks() -> None:
    """服务关闭:取消所有运行中的定时发送协程(不改 DB 状态)。

    定时任务用 asyncio.create_task 自管理,不在 task_tracker 内,关闭时必须显式
    取消,否则这些 while True 协程不会结束,uvicorn 会一直卡在
    "Waiting for background tasks to complete"。
    """
    phones = list(_tasks.keys())
    for phone in phones:
        await _cancel_task(phone)
    if phones:
        logger.info("已取消 %d 个运行中的定时发送任务", len(phones))


async def stop_all_on_startup() -> None:
    """服务启动:DB 全置停(不自动恢复任务)。"""
    await run_db(schedule_repo.stop_all_on_startup)
