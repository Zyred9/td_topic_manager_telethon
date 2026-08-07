"""小号健康巡检:周期检测在线/授权状态并同步实时资料。

解决两个运营痛点:
- 死号检测(问题1):运行中的号掉线/被封后,DB 状态不会自动变化,运营只能逐个手点
  排查。本服务周期性把真实状态回写 DB,运营刷新列表即可看到离线/需重登的号。
- 资料同步(问题5):用户在 TG 改了 username/昵称后台不更新,难以定位到号。巡检顺便
  拉取 get_me() 比对回写。

巡检对象包含 ClientManager 池内的号，以及 DB 中已登录/需重登但意外掉出池的号。
用户主动下线的号不自动重连。生命周期(start/stop)对齐 topic_scheduler。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from config.constants import AccountStatus
from config.settings import get_settings
from core.client_manager import client_manager
from helpers.dead_account import (
    account_state_lock,
    is_dead_error,
    mark_dead_and_remove,
    pending_dead_phones,
    retry_pending_dead_marks,
)
from infra.db import run_db
from repositories import account_repo

logger = logging.getLogger(__name__)

# 号间巡检抖动(秒),避免短时间密集请求触发风控
_JITTER_MIN_SEC = 1.0
_JITTER_MAX_SEC = 2.0

# 临时网络抖动不能直接让账号掉线；同一个 client 连续失败两轮才置需重登。
_AMBIGUOUS_FAIL_CONFIRM_ROUNDS = 2
_probe_fail_streak: dict[str, tuple[object | None, int]] = {}

_watch_task: Optional[asyncio.Task] = None


async def _mark_dead(phone: str, exc: BaseException, expected_client, expected_statuses) -> None:
    """命中终态死号异常:走公共口径标死并出池。"""
    logger.warning("巡检判定死号 phone=%s reason=%s", phone, type(exc).__name__, exc_info=exc)
    marked = await mark_dead_and_remove(
        phone,
        exc,
        expected_client=expected_client,
        expected_statuses=expected_statuses,
    )
    if marked:
        _probe_fail_streak.pop(phone, None)


async def _on_ambiguous_fail(
    phone: str,
    exc: BaseException,
    expected_client,
    expected_statuses,
) -> None:
    """模糊探活失败只置需重登并出池，后续使用新 client 重连。"""
    async with account_state_lock():
        current_client = client_manager.get_client(phone)
        if current_client is not expected_client:
            logger.info("巡检旧 client 失败结果已过期,跳过状态变更 phone=%s", phone)
            return
        streak_client, previous_streak = _probe_fail_streak.get(phone, (expected_client, 0))
        streak = previous_streak + 1 if streak_client is expected_client else 1
        if streak < _AMBIGUOUS_FAIL_CONFIRM_ROUNDS:
            _probe_fail_streak[phone] = (expected_client, streak)
            logger.warning(
                "巡检探活失败(第 %d/%d 轮),暂不下线 phone=%s",
                streak,
                _AMBIGUOUS_FAIL_CONFIRM_ROUNDS,
                phone,
                exc_info=exc,
            )
            return

        _probe_fail_streak.pop(phone, None)
        logger.warning(
            "巡检探活连续 %d 轮失败,置需重登并出池 phone=%s",
            streak,
            phone,
            exc_info=exc,
        )
        should_remove = True
        try:
            if expected_statuses is None:
                await run_db(account_repo.update_status, phone, int(AccountStatus.NEED_RELOGIN))
            else:
                affected = await run_db(
                    account_repo.update_status_if_current,
                    phone,
                    int(AccountStatus.NEED_RELOGIN),
                    expected_statuses,
                )
                if not affected:
                    should_remove = False
                    return
        finally:
            if should_remove:
                await client_manager.remove(phone)


async def _check_one(phone: str, account=None) -> None:
    """巡检单个号:真发一次网络请求探活,据结果判死/置需重登/同步资料。"""
    client = client_manager.get_ready_client(phone)
    if client is None:
        # 池里有该号但连接已断:尝试重连一次
        raw = client_manager.get_client(phone)
        if raw is None:
            if account is None:
                return
            try:
                await asyncio.wait_for(
                    client_manager.connect_account(account),
                    timeout=client_manager.CONNECT_TIMEOUT,
                )
            except Exception as exc:
                if is_dead_error(exc):
                    await _mark_dead(
                        phone,
                        exc,
                        expected_client=None,
                        expected_statuses=(int(account.status),),
                    )
                else:
                    await _on_ambiguous_fail(
                        phone,
                        exc,
                        expected_client=None,
                        expected_statuses=(int(account.status),),
                    )
            else:
                _probe_fail_streak.pop(phone, None)
            return
        try:
            await raw.connect()
        except Exception as exc:
            if is_dead_error(exc):
                await _mark_dead(phone, exc, expected_client=raw, expected_statuses=None)
            else:
                await _on_ambiguous_fail(
                    phone,
                    exc,
                    expected_client=raw,
                    expected_statuses=None,
                )
            return
        client = raw if raw.is_connected() else None
        if client is None:
            await _on_ambiguous_fail(
                phone,
                RuntimeError("重连后仍未连接"),
                expected_client=raw,
                expected_statuses=None,
            )
            return

    # 探活:必须真发一次需鉴权的请求。不能用 is_user_authorized() —— 它读 client
    # 内存里的 _authorized 缓存,号进池时已被设为 True 后常驻,session 远端被作废
    # 也察觉不到(这正是历史上死号列表始终为空的根因)。get_me() 每次真发 RPC,
    # 远端失效会抛 UnauthorizedError 系异常,据此判死。
    try:
        me = await client.get_me()
        frozen = await client_manager.mark_frozen_if_needed(
            phone,
            client,
            me,
            expected_client=client,
        )
    except Exception as exc:
        if is_dead_error(exc):
            await _mark_dead(phone, exc, expected_client=client, expected_statuses=None)
        else:
            await _on_ambiguous_fail(
                phone,
                exc,
                expected_client=client,
                expected_statuses=None,
            )
        return
    if frozen:
        _probe_fail_streak.pop(phone, None)
        return
    if me is None:
        # 拿不到自身实体:视作未授权,按模糊失败累计(不直接判死,留重登余地)
        await _on_ambiguous_fail(
            phone,
            RuntimeError("get_me() 返回 None,疑似未授权"),
            expected_client=client,
            expected_statuses=None,
        )
        return

    # 探活成功:同步实时资料(问题5)
    _probe_fail_streak.pop(phone, None)
    await _sync_profile(phone, me)


async def _sync_profile(phone: str, me) -> None:
    """用已探活拿到的 me 比对 DB,有变化才回写 username/昵称(问题5)。"""
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
        # touch_edit_time=False:巡检自动同步不是 web 编辑资料,不刷 update_time,
        # 否则会把在 TG 改过名的号误判为「改过资料」排到列表最后,污染排序。
        await run_db(account_repo.update_profile, phone, new_first, new_last, new_username, None,
                     touch_edit_time=False)
        logger.info(
            "巡检同步资料 phone=%s 昵称 %r->%r 用户名 %r->%r",
            phone, acc.tg_first_name, new_first, acc.tg_username, new_username,
        )


async def _watch_tick() -> None:
    """一轮巡检:检查池内号，并补连意外掉出池的已登录/需重登账号。"""
    await retry_pending_dead_marks()
    pending_phones = pending_dead_phones()
    accounts_by_phone = {}
    try:
        accounts = await run_db(
            account_repo.find_by_statuses,
            [int(AccountStatus.LOGGED_IN), int(AccountStatus.NEED_RELOGIN)],
        )
        accounts_by_phone = {
            account.phone: account for account in accounts
            if account.phone not in pending_phones
        }
    except Exception:
        logger.exception("巡检查询池外候选失败,本轮仅检查池内号")
    phones = [
        phone for phone in dict.fromkeys([*client_manager.all_phones(), *accounts_by_phone])
        if phone not in pending_phones
    ]
    if not phones:
        return
    logger.info("小号巡检开始,本轮 %d 个号", len(phones))
    for phone in phones:
        try:
            await _check_one(phone, accounts_by_phone.get(phone))
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
