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
from typing import Dict, Optional

from config.constants import AccountStatus
from config.settings import get_settings
from core.client_manager import client_manager
from helpers.dead_account import is_dead_error, mark_dead_and_remove, mark_dead_by_reason
from infra.db import run_db
from repositories import account_repo, send_log_repo

logger = logging.getLogger(__name__)

# 号间巡检抖动(秒),避免短时间密集请求触发风控
_JITTER_MIN_SEC = 1.0
_JITTER_MAX_SEC = 2.0

# 误杀防护:模糊失败(超时/连不上/非终态 RPC)连续这么多轮巡检都失败才判死。
# 明确的终态死号异常(session 作废/账号封禁)不走累计,立即判死。
_DEAD_CONFIRM_ROUNDS = 2

# phone -> 连续模糊探活失败轮数。探活成功 / 判死 / 复活后清零。进程内即可,
# 重启后从 0 重新累计,不需要落库。
_probe_fail_streak: Dict[str, int] = {}

_watch_task: Optional[asyncio.Task] = None


def _clear_streak(phone: str) -> None:
    _probe_fail_streak.pop(phone, None)


async def _mark_dead(phone: str, exc: BaseException) -> None:
    """命中终态死号异常:走公共口径标死+出池,并清本服务的失败计数。"""
    logger.warning("巡检判定死号 phone=%s reason=%s", phone, type(exc).__name__, exc_info=exc)
    await mark_dead_and_remove(phone, exc)
    _clear_streak(phone)


async def _on_ambiguous_fail(phone: str, exc: BaseException) -> None:
    """模糊探活失败(超时/连不上/非终态 RPC):累计;连续够轮数才判死,中间先置离线。

    防止偶发网络抖动把好号误杀进死号列表。明确终态异常不走这里(直接判死)。
    """
    streak = _probe_fail_streak.get(phone, 0) + 1
    _probe_fail_streak[phone] = streak
    if streak >= _DEAD_CONFIRM_ROUNDS:
        logger.warning("巡检探活连续 %d 轮失败,判定死号 phone=%s reason=%s",
                       streak, phone, type(exc).__name__, exc_info=exc)
        await mark_dead_and_remove(phone, exc)
        _clear_streak(phone)
    else:
        logger.warning("巡检探活失败(第 %d/%d 轮),先置离线观察 phone=%s",
                       streak, _DEAD_CONFIRM_ROUNDS, phone, exc_info=exc)
        await run_db(account_repo.update_status, phone, int(AccountStatus.OFFLINE))


async def _check_one(phone: str) -> None:
    """巡检单个号:真发一次网络请求探活,据结果判死/置离线/同步资料。"""
    client = client_manager.get_ready_client(phone)
    if client is None:
        # 池里有该号但连接已断:尝试重连一次
        raw = client_manager.get_client(phone)
        if raw is None:
            return
        try:
            await raw.connect()
        except Exception as exc:
            # 连不上属模糊失败,走累计判死(可能只是临时网络问题)
            await _on_ambiguous_fail(phone, exc)
            return
        client = raw if raw.is_connected() else None
        if client is None:
            logger.warning("巡检重连后仍未连接,置离线 phone=%s", phone)
            await run_db(account_repo.update_status, phone, int(AccountStatus.OFFLINE))
            return

    # 探活:必须真发一次需鉴权的请求。不能用 is_user_authorized() —— 它读 client
    # 内存里的 _authorized 缓存,号进池时已被设为 True 后常驻,session 远端被作废
    # 也察觉不到(这正是历史上死号列表始终为空的根因)。get_me() 每次真发 RPC,
    # 远端失效会抛 UnauthorizedError 系异常,据此判死。
    try:
        me = await client.get_me()
    except Exception as exc:
        if is_dead_error(exc):
            await _mark_dead(phone, exc)
        else:
            await _on_ambiguous_fail(phone, exc)
        return
    if me is None:
        # 拿不到自身实体:视作未授权,按模糊失败累计(不直接判死,留重登余地)
        await _on_ambiguous_fail(phone, RuntimeError("get_me() 返回 None,疑似未授权"))
        return

    # 探活成功:清零失败计数,同步实时资料(问题5)
    _clear_streak(phone)
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


async def scan_dead_on_startup() -> None:
    """启动扫描:把 t_send_log 里累计「被群封」≥ 阈值的号一次性判死搬进死号列表。

    补「死号列表为空」的根因:实时判死只在发送那一刻生效,历史日志里已有的 UserBanned
    失败无人回看 → 死号永远标不上。启动时按发送日志统计一次补判死。口径与实时一致:
    只数 UserBannedInChannelError,不误杀被禁言/限流的号。mark_dead_by_reason 幂等。
    """
    threshold = get_settings().watch.startup_fail_count
    phones = await run_db(send_log_repo.phones_with_banned_count_at_least, threshold)
    if not phones:
        logger.info("启动扫描:无累计被群封 ≥ %d 次的号", threshold)
        return
    logger.info("启动扫描:%d 个号累计被群封 ≥ %d 次,判死", len(phones), threshold)
    for phone in phones:
        await mark_dead_by_reason(
            phone, "UserBannedInChannelError", f"启动扫描:累计被群封 ≥ {threshold} 次",
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
