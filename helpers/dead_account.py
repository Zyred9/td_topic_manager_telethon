"""死号判定口径(单一真源)。

「死号」= session 被作废 / 账号被封 / authkey 失效等**不可恢复终态**,需运营在
死号列表里复活(重登)或彻底删除。判定口径必须全项目统一,故收口到此处,
message_sender(发送链路)与 account_watch_service(巡检链路)共用,不得各写一份。

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

from telethon import errors

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
