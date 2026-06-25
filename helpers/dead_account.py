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

import logging

from telethon import errors

logger = logging.getLogger(__name__)

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


def is_send_dead_error(exc: BaseException) -> bool:
    """发送链路失败时,该异常是否要判死整号(出池、进死号列表)。

    口径(运营锁定):
    - 终态 session 失效(is_dead_error:封号/作废)→ 判死。
    - UserBannedInChannelError(被该群永久封禁)→ 判死。运营要求这类进死号列表。
    其余一切失败(被禁言 ChatWriteForbidden / 限流 / 超时 / 群私有等)返回 False,
    不判死号 —— 由调用方(定时任务)按「只停该群」处理,不连累整号。
    """
    if is_dead_error(exc):
        return True
    return isinstance(exc, errors.UserBannedInChannelError)


async def mark_dead_and_remove(phone: str, exc: BaseException) -> bool:
    """死号统一落库口径(全项目唯一实现):标 is_dead=1 + 移出 client 池。

    所有链路(发送 / 巡检 / 启动 / 登录 / 导入 / 群操作 / 移交群主 / 私聊下载)捕获到
    is_dead_error(exc)==True 的终态异常后,都应调用本函数,不得各写一份(铁律:禁止重复)。
    幂等:mark_dead 内 WHERE is_dead=0 保证只标第一次。返回 True 表示本次确实把号标进了
    死号集合(affected>0),调用方可据此决定给运营的提示文案。失败仅记日志、不抛,绝不阻塞
    调用方主流程。

    延迟 import account_repo / client_manager:本模块被 message_sender 等底层模块依赖,
    模块级 import 会形成循环,放函数内规避。
    """
    return await _mark_and_remove(phone, type(exc).__name__, str(exc))


async def mark_dead_by_reason(phone: str, err_code: str, err_desc: str) -> bool:
    """无异常对象时的判死入口(如启动扫描按发送日志统计判死)。

    与 mark_dead_and_remove 共用唯一落库+出池实现,只是 reason 由调用方按业务语义给,
    不是从异常派生。幂等(WHERE is_dead=0)。
    """
    return await _mark_and_remove(phone, err_code, err_desc)


async def _mark_and_remove(phone: str, err_code: str, err_desc: str) -> bool:
    """死号落库的唯一实现:标 is_dead=1(幂等,WHERE is_dead=0)+ 移出 client 池。
    失败仅记日志、不抛,绝不阻塞调用方主流程。"""
    from core.client_manager import client_manager
    from infra.db import run_db
    from repositories import account_repo

    marked = False
    try:
        affected = await run_db(account_repo.mark_dead, phone, err_code, err_desc)
        marked = bool(affected)
        if marked:
            logger.warning("[死号] phone=%s 已标记失效 reason=%s", phone, err_code)
    except Exception as e2:
        logger.warning("[死号] 标记失败 phone=%s: %s", phone, e2)
    try:
        await client_manager.remove(phone)
    except Exception as e2:
        logger.warning("[死号] 移出 client 池失败 phone=%s: %s", phone, e2)
    return marked
