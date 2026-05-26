"""Telethon 异常 → 中文运营可读文案。

加群/退群/资料编辑等接口返回的 failReason 经此映射;原始错误保留在日志,
前端只看中文。FloodWait 带剩余秒数。
"""

from __future__ import annotations

import re

from telethon import errors

# 关键字片段 → 中文(按 RPCError.message 或异常类名匹配)
_MESSAGE_MAP: dict[str, str] = {
    "USER_PRIVACY_RESTRICTED": "对方设置了隐私限制,无法加入或邀请",
    "INVITE_HASH_EXPIRED": "邀请链接已失效",
    "INVITE_HASH_INVALID": "邀请链接无效",
    "USER_BANNED_IN_CHANNEL": "已被该群封禁",
    "USER_ALREADY_PARTICIPANT": "已在群内",
    "CHANNELS_TOO_MUCH": "加入的群/频道已达上限",
    "CHANNEL_PRIVATE": "该群为私有或已无权访问",
    "USER_NOT_PARTICIPANT": "不在该群内",
    "CHAT_ADMIN_REQUIRED": "需要管理员权限",
    "USERNAME_NOT_OCCUPIED": "该用户名不存在",
    "USERNAME_INVALID": "用户名格式无效",
    "PEER_FLOOD": "操作过于频繁,账号被临时限制",
    "PHONE_NUMBER_BANNED": "该手机号已被封禁",
    "SESSION_REVOKED": "会话已失效,需重新登录",
    "AUTH_KEY_UNREGISTERED": "登录已失效,需重新登录",
    "PHONE_CODE_INVALID": "验证码错误",
    "PHONE_CODE_EXPIRED": "验证码已过期",
    "PASSWORD_HASH_INVALID": "二级密码错误",
    "FRESH_RESET_AUTHORISATION_FORBIDDEN": "新会话暂不允许此操作,请稍后再试",
}

_FLOOD_RE = re.compile(r"(\d+)")


def translate(exc: BaseException) -> str:
    """把 Telethon/通用异常转中文运营文案。"""
    # FloodWait:带剩余秒数
    if isinstance(exc, errors.FloodWaitError):
        return f"操作频率过高,请等待 {exc.seconds} 秒后重试"
    if isinstance(exc, errors.SessionPasswordNeededError):
        return "该账号开启了二级密码(2FA),请提交 2FA 密码"
    if isinstance(exc, errors.PhoneCodeInvalidError):
        return "验证码错误"
    if isinstance(exc, errors.PhoneCodeExpiredError):
        return "验证码已过期,请重新获取"

    raw = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
    upper = raw.upper()
    for key, zh in _MESSAGE_MAP.items():
        if key in upper:
            # FLOOD_WAIT_X 形式带秒数
            if key == "PEER_FLOOD":
                return zh
            return zh
    # FLOOD_WAIT_123 文本形式
    if "FLOOD_WAIT" in upper:
        m = _FLOOD_RE.search(raw)
        sec = m.group(1) if m else "?"
        return f"操作频率过高,请等待 {sec} 秒后重试"
    return raw
