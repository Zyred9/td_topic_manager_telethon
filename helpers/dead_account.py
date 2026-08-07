"""死号判定口径(单一真源)。

「死号」= session 被作废 / 账号被封 / authkey 失效等**不可恢复终态**,需运营在
死号列表里复活(重登)或彻底删除。判定口径必须全项目统一,故收口到此处,
发送链路(message_sender)/巡检链路(get_me 探活)/启动全起等共用,不得各写一份。
注:普通「发送失败」(被群封/被禁言/限流等)不判死,由调用方各自处理(如定时任务停该群)。

口径(Telethon 1.43.2 实测类继承关系):
- errors.UnauthorizedError 基类:一网打尽 AuthKeyUnregistered / AuthKeyInvalid /
  SessionRevoked / SessionExpired / UserDeactivated / UserDeactivatedBan 等 6 个
  session/授权终态失效。
- AuthKeyDuplicatedError:基类是 AuthKeyError 不在 UnauthorizedError 下,需单列;
  同一 session 多 IP 撞车被 TG 作废,本项目现网最常见死法。
- PhoneNumberBannedError:手机号被封,单列。

**显式排除**(虽属 UnauthorizedError 子类但号本身是好的,判死即误杀):
- SessionPasswordNeededError:代表"需要输入 2FA 密码",账号正常,绝不判死。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Collection

from telethon import errors

logger = logging.getLogger(__name__)

# ponytail: 判死与授权注册都是低频状态转换，一个全局锁足够，也更容易保证顺序一致。
_account_state_lock = asyncio.Lock()
_pending_dead: dict[str, tuple[object, str, str, tuple[int, ...] | None]] = {}
_EXPECTED_CLIENT_UNSET = object()

# 不在 UnauthorizedError 基类下、但仍属终态失效,需显式补充的异常类型
_EXTRA_DEAD_ERRORS: tuple[type[BaseException], ...] = (
    errors.AuthKeyDuplicatedError,
    errors.PhoneNumberBannedError,
)


def is_dead_error(exc: BaseException) -> bool:
    """判断异常是否代表小号已进入不可恢复终态(死号)。

    SessionPasswordNeededError 虽是 UnauthorizedError 子类,但代表号正常、只需补 2FA,
    必须先排除,否则会把开了二级密码的好号误判为死号。
    """
    if isinstance(exc, errors.SessionPasswordNeededError):
        return False
    if isinstance(exc, errors.UnauthorizedError):
        return True
    return isinstance(exc, _EXTRA_DEAD_ERRORS)


def account_state_lock() -> asyncio.Lock:
    """返回判死/授权注册共享的账号状态转换锁。"""
    return _account_state_lock


async def mark_dead_and_remove(
    phone: str,
    exc: BaseException,
    expected_client: Any = _EXPECTED_CLIENT_UNSET,
    expected_statuses: Collection[int] | None = None,
) -> bool:
    """死号统一落库口径(全项目唯一实现):标 is_dead=1 + 移出 client 池。

    所有链路(发送 / 巡检 / 启动 / 登录 / 导入 / 群操作 / 移交群主 / 私聊下载)捕获到
    is_dead_error(exc)==True 的终态异常后,都应调用本函数,不得各写一份(铁律:禁止重复)。
    幂等:mark_dead 内 WHERE is_dead=0 保证只标第一次。返回 True 表示本次确实把号标进了
    死号集合(affected>0),调用方可据此决定给运营的提示文案。失败仅记日志、不抛,绝不阻塞
    调用方主流程。

    延迟 import account_repo / client_manager:本模块被 message_sender 等底层模块依赖,
    模块级 import 会形成循环,放函数内规避。
    """
    return await _mark_and_remove(
        phone,
        type(exc).__name__,
        str(exc),
        expected_client=expected_client,
        expected_statuses=expected_statuses,
    )


async def mark_dead_by_reason(
    phone: str,
    err_code: str,
    err_desc: str,
    expected_client: Any = _EXPECTED_CLIENT_UNSET,
    expected_statuses: Collection[int] | None = None,
) -> bool:
    """无异常对象时的判死入口(如启动扫描按发送日志统计判死)。

    与 mark_dead_and_remove 共用唯一落库+出池实现,只是 reason 由调用方按业务语义给,
    不是从异常派生。幂等(WHERE is_dead=0)。
    """
    return await _mark_and_remove(
        phone,
        err_code,
        err_desc,
        expected_client=expected_client,
        expected_statuses=expected_statuses,
    )


async def _mark_and_remove(
    phone: str,
    err_code: str,
    err_desc: str,
    expected_client: Any = _EXPECTED_CLIENT_UNSET,
    expected_statuses: Collection[int] | None = None,
    pending_token: object | None = None,
) -> bool:
    """死号落库的唯一实现:标 is_dead=1(幂等,WHERE is_dead=0)+ 移出 client 池。
    失败仅记日志、不抛,绝不阻塞调用方主流程。"""
    from core.client_manager import client_manager
    from infra.db import run_db
    from repositories import account_repo

    async with _account_state_lock:
        pending = _pending_dead.get(phone)
        if pending_token is not None and (pending is None or pending[0] is not pending_token):
            return False

        current_client = client_manager.get_client(phone)
        if expected_client is not _EXPECTED_CLIENT_UNSET and current_client is not expected_client:
            return False

        normalized_statuses = (
            None
            if expected_statuses is None
            else tuple(sorted({int(status) for status in expected_statuses}))
        )
        marked = False
        try:
            affected = await run_db(
                account_repo.mark_dead,
                phone,
                err_code,
                err_desc,
                normalized_statuses,
            )
            marked = bool(affected)
            _pending_dead.pop(phone, None)
            if not marked:
                return False
            logger.warning("[死号] phone=%s 已标记失效 reason=%s", phone, err_code)
        except Exception as e2:
            logger.warning("[死号] 标记失败 phone=%s: %s", phone, e2)
            token = pending_token if pending_token is not None else object()
            _pending_dead[phone] = (token, err_code, err_desc, normalized_statuses)
        try:
            await client_manager.remove(phone)
        except Exception as e2:
            logger.warning("[死号] 移出 client 池失败 phone=%s: %s", phone, e2)
        return marked


def pending_dead_phones() -> set[str]:
    """返回尚未成功落库的死号手机号快照。"""
    return set(_pending_dead)


def clear_pending_dead(phone: str) -> None:
    """新授权 client 成功接管后清除旧 session 遗留的待标死记录。"""
    _pending_dead.pop(phone, None)


async def retry_pending_dead_marks() -> None:
    """重试此前因数据库异常未落库的死号标记。"""
    for phone, pending in list(_pending_dead.items()):
        token, err_code, err_desc, expected_statuses = pending
        await _mark_and_remove(
            phone,
            err_code,
            err_desc,
            expected_statuses=expected_statuses,
            pending_token=token,
        )
