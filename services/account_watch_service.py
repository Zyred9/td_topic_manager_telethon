"""小号健康巡检:周期检测在线/授权状态并同步实时资料。

解决两个运营痛点:
- 死号检测(问题1):运行中的号掉线/被封后,DB 状态不会自动变化,运营只能逐个手点
  排查。本服务周期性把真实状态回写 DB,运营刷新列表即可看到离线/需重登的号。
- 资料同步(问题5):用户在 TG 改了 username/昵称后台不更新,难以定位到号。巡检顺便
  拉取 get_me() 比对回写。

巡检对象是 ClientManager 池内的号(即已登录、正在运行的号)。掉线/被封正发生在这些
号上;池外的号本就是离线/需重登态,无需巡检。生命周期(start/stop)对齐 topic_scheduler。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from telethon import errors

from config.constants import AccountStatus
from config.settings import get_settings
from core.client_manager import client_manager
from infra.db import run_db
from repositories import account_repo

logger = logging.getLogger(__name__)

# 号间巡检抖动(秒),避免短时间密集请求触发风控
_JITTER_MIN_SEC = 1.0
_JITTER_MAX_SEC = 2.0

# session 被作废 / 账号失效的明确终态异常 → 判为"需重新登录"(死号)
_DEAD_ERRORS = (
    errors.AuthKeyDuplicatedError,
    errors.AuthKeyUnregisteredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)

_watch_task: Optional[asyncio.Task] = None


async def _check_one(phone: str) -> None:
    """巡检单个号:检测连接/授权状态并回写,在线则同步实时资料。"""
    client = client_manager.get_ready_client(phone)
    if client is None:
        # 池里有该号但连接已断:尝试重连一次,失败则置离线
        raw = client_manager.get_client(phone)
        if raw is None:
            return
        try:
            await raw.connect()
        except Exception as exc:
            logger.warning("巡检重连失败,置离线 phone=%s", phone, exc_info=exc)
            await run_db(account_repo.update_status, phone, int(AccountStatus.OFFLINE))
            return
        client = raw if raw.is_connected() else None
        if client is None:
            logger.warning("巡检重连后仍未连接,置离线 phone=%s", phone)
            await run_db(account_repo.update_status, phone, int(AccountStatus.OFFLINE))
            return

    # 授权校验:失效则判为死号(不可恢复异常),移出池并搬到死号列表
    try:
        authorized = await client.is_user_authorized()
    except _DEAD_ERRORS as exc:
        logger.warning("巡检发现 session 已失效(被作废/封禁),标记死号 phone=%s", phone, exc_info=exc)
        await run_db(account_repo.mark_dead, phone, type(exc).__name__, str(exc))
        await client_manager.remove(phone)
        return
    if not authorized:
        # 未授权但没抛具体死号异常:仍按需重登处理,不进死号列表(运营走重登可恢复)
        logger.warning("巡检发现未授权,需重新登录 phone=%s", phone)
        await run_db(account_repo.update_status, phone, int(AccountStatus.NEED_RELOGIN))
        await client_manager.remove(phone)
        return

    # 在线且授权正常:同步实时资料(问题5)
    await _sync_profile(phone, client)


async def _sync_profile(phone: str, client) -> None:
    """拉 get_me() 比对 DB,有变化才回写 username/昵称(问题5)。"""
    try:
        me = await client.get_me()
    except _DEAD_ERRORS as exc:
        logger.warning("拉取资料发现 session 已失效,标记死号 phone=%s", phone, exc_info=exc)
        await run_db(account_repo.mark_dead, phone, type(exc).__name__, str(exc))
        await client_manager.remove(phone)
        return
    if me is None:
        return

    acc = await run_db(account_repo.find_by_phone, phone)
    if acc is None:
        return

    new_first = getattr(me, "first_name", None)
    new_last = getattr(me, "last_name", None)
    new_username = getattr(me, "username", None)
    # 仅在有变化时回写(update_profile 跳过 None,不会误清空)
    changed = (
        new_first != acc.tg_first_name
        or new_last != acc.tg_last_name
        or new_username != acc.tg_username
    )
    if changed:
        await run_db(account_repo.update_profile, phone, new_first, new_last, new_username, None)
        logger.info(
            "巡检同步资料 phone=%s 昵称 %r->%r 用户名 %r->%r",
            phone, acc.tg_first_name, new_first, acc.tg_username, new_username,
        )


async def _watch_tick() -> None:
    """一轮巡检:逐个检查池内号,号间抖动,单号异常隔离不中断整轮。"""
    phones = client_manager.all_phones()
    if not phones:
        return
    logger.info("小号巡检开始,本轮 %d 个号", len(phones))
    for phone in phones:
        try:
            await _check_one(phone)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("巡检单号异常,跳过 phone=%s", phone, exc_info=exc)
        await asyncio.sleep(random.uniform(_JITTER_MIN_SEC, _JITTER_MAX_SEC))


async def _watch_loop() -> None:
    interval_sec = get_settings().watch.interval_min * 60
    while True:
        try:
            await _watch_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("小号巡检整轮异常")
        await asyncio.sleep(interval_sec)


def start_watch() -> None:
    global _watch_task
    if _watch_task is None or _watch_task.done():
        _watch_task = asyncio.create_task(_watch_loop())
        logger.info("小号健康巡检已启动,间隔 %d 分钟", get_settings().watch.interval_min)


async def stop_watch() -> None:
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        _watch_task.cancel()
        try:
            await _watch_task
        except (asyncio.CancelledError, Exception):
            pass
