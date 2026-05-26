"""AI 话题:CRUD + 建群 + 在群校验 + 乐观锁 + 移交群主。

建群:由 owner 小号(成员中挑选)用 Telethon 原生 CreateChannel(megagroup) +
ToggleForum 开 Forum。Bot 不参与。已有群则只校验在群。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from telethon import errors
from telethon.tl import functions, types
from telethon.tl.tlobject import TLRequest
from telethon.utils import get_peer_id

from config.constants import TaskStatus
from core.client_manager import client_manager
from helpers import td_error
from infra.db import run_db
from repositories import account_watch_repo, topic_member_repo, topic_repo

logger = logging.getLogger(__name__)


class _EditCreatorRequest(TLRequest):
    """移交超级群所有权(channels.editCreator)。

    Telethon 1.43.2 的打包 schema 缺失 channels.EditCreatorRequest,这里按
    官方 TL 定义手工复刻(constructor id 0x8f38cd1f,返回 Updates),
    不依赖库里是否存在该类。传入参数需已是 InputChannel / InputUser /
    InputCheckPasswordSRP(本类不做 resolve)。
    """

    CONSTRUCTOR_ID = 0x8f38cd1f
    SUBCLASS_OF_ID = 0x8af52aac

    def __init__(self, channel, user_id, password):
        self.channel = channel
        self.user_id = user_id
        self.password = password

    def to_dict(self):
        return {
            "_": "EditCreatorRequest",
            "channel": self.channel,
            "user_id": self.user_id,
            "password": self.password,
        }

    def _bytes(self):
        return b"".join((
            b"\x1f\xcd8\x8f",
            self.channel._bytes(),
            self.user_id._bytes(),
            self.password._bytes(),
        ))

    @classmethod
    def from_reader(cls, reader):
        _channel = reader.tgread_object()
        _user_id = reader.tgread_object()
        _password = reader.tgread_object()
        return cls(channel=_channel, user_id=_user_id, password=_password)


_DEFAULTS = {
    "message_thread_id": 0, "speak_min_sec": 30, "speak_max_sec": 180,
    "reply_user_prob": 30, "max_per_min": 6, "reply_min_chars": 8,
    "reply_max_chars": 25, "filler_enabled": 1, "filler_prob": 10,
}


class TopicError(Exception):
    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"


def _row_to_vo(row: dict, phones: List[str]) -> dict:
    return {
        "id": row["id"],
        "topicName": row["topic_name"],
        "topicPrompt": row["topic_prompt"],
        "ownerPhone": row["owner_phone"],
        "chatId": row["chat_id"],
        "messageThreadId": row["message_thread_id"],
        "speakMinSec": row["speak_min_sec"],
        "speakMaxSec": row["speak_max_sec"],
        "replyUserProb": row["reply_user_prob"],
        "maxPerMin": row["max_per_min"],
        "replyMinChars": row["reply_min_chars"],
        "replyMaxChars": row["reply_max_chars"],
        "fillerEnabled": row["filler_enabled"],
        "fillerProb": row["filler_prob"],
        "status": row["status"],
        "version": row["version"],
        "participantCnt": len(phones),
        "phones": phones,
    }


async def list_topics() -> List[dict]:
    rows = await run_db(topic_repo.list_all)
    result = []
    for row in rows:
        phones = await run_db(topic_member_repo.phones_of_topic, row["id"])
        result.append(_row_to_vo(row, phones))
    return result


async def list_forum_chats() -> List[dict]:
    """供前端"在已有 Forum 群下新建子话题"下拉:返回去重的 Forum 群 [{chatId, chatTitle}]。"""
    rows = await run_db(topic_repo.list_forum_chats)
    return [
        {"chatId": r["chat_id"], "chatTitle": r.get("chat_title") or str(r["chat_id"])}
        for r in rows
    ]


async def get_topic(topic_id: int) -> dict:
    row = await run_db(topic_repo.find_by_id, topic_id)
    if row is None:
        raise TopicError("话题不存在", code=404)
    phones = await run_db(topic_member_repo.phones_of_topic, topic_id)
    return _row_to_vo(row, phones)


def _validate_chars(min_c: int, max_c: int) -> None:
    if not (1 <= min_c <= max_c <= 50):
        raise TopicError("回复字数需满足 1 ≤ 下限 ≤ 上限 ≤ 50")


async def create_topic(req) -> dict:
    phones = [_normalize(p) for p in (req.phones or [])]
    if not phones:
        raise TopicError("至少选择一个参与小号")

    cfg = {k: (getattr(req, _camel(k)) if getattr(req, _camel(k)) is not None else v)
           for k, v in _DEFAULTS.items()}
    _validate_chars(cfg["reply_min_chars"], cfg["reply_max_chars"])

    # 群入口模式:兼容旧调用(无 chatMode 时按有无 chatId 推断)
    mode = req.chatMode or ("existing_chat" if req.chatId is not None else "new_forum")

    if mode == "new_forum":
        # 新建 Forum 群 + 建首个子话题
        owner_phone, chat_id, thread_id, members = await _create_group(
            req.topicName, req.topicPrompt, req.topicName, phones,
        )
        skipped = [p for p in phones if p not in members]

    elif mode == "existing_forum":
        # 已有 Forum 群下新建子话题
        if req.chatId is None:
            raise TopicError("请选择已有的 Forum 群")
        in_phones, skipped = await _filter_in_group(phones, req.chatId)
        if not in_phones:
            raise TopicError("所选小号均不在该群,无法创建话题")
        chat_id = req.chatId
        owner_phone = await _pick_owner(in_phones)
        owner_client = client_manager.get_ready_client(owner_phone)
        peer = await owner_client.get_input_entity(chat_id)
        thread_id = await _create_forum_topic(owner_client, peer, req.topicName)
        if not thread_id:
            raise TopicError("在该群创建子话题失败,请确认它是 Forum 群且小号有权限")
        members = in_phones
        for p in members:
            await run_db(account_watch_repo.add, p, chat_id)

    else:  # existing_chat:接入已有普通群(不带子话题)
        if req.chatId is None:
            raise TopicError("请填写群 ID")
        in_phones, skipped = await _filter_in_group(phones, req.chatId)
        if not in_phones:
            raise TopicError("所选小号均不在该群,无法创建话题")
        chat_id = req.chatId
        owner_phone = await _pick_owner(in_phones)
        thread_id = req.messageThreadId or 0
        members = in_phones
        for p in members:
            await run_db(account_watch_repo.add, p, chat_id)

    data = {
        "topic_name": req.topicName, "topic_prompt": req.topicPrompt,
        "owner_phone": owner_phone, "chat_id": chat_id, "message_thread_id": thread_id,
        "speak_min_sec": cfg["speak_min_sec"], "speak_max_sec": cfg["speak_max_sec"],
        "reply_user_prob": cfg["reply_user_prob"], "max_per_min": cfg["max_per_min"],
        "reply_min_chars": cfg["reply_min_chars"], "reply_max_chars": cfg["reply_max_chars"],
        "filler_enabled": cfg["filler_enabled"], "filler_prob": cfg["filler_prob"],
    }
    topic_id = await run_db(topic_repo.insert, data)
    await run_db(topic_member_repo.add_members, topic_id, members)
    logger.info("创建话题 id=%s chat=%s owner=%s 成员=%d 剔除=%d",
                topic_id, chat_id, owner_phone, len(members), len(skipped))
    vo = await get_topic(topic_id)
    vo["skipped"] = skipped  # 附带被剔除的号(前端可选展示)
    return vo


async def update_topic(topic_id: int, req) -> dict:
    row = await run_db(topic_repo.find_by_id, topic_id)
    if row is None:
        raise TopicError("话题不存在", code=404)

    fields: dict = {}
    mapping = {
        "topicName": "topic_name", "topicPrompt": "topic_prompt",
        "speakMinSec": "speak_min_sec", "speakMaxSec": "speak_max_sec",
        "replyUserProb": "reply_user_prob", "maxPerMin": "max_per_min",
        "replyMinChars": "reply_min_chars", "replyMaxChars": "reply_max_chars",
        "fillerEnabled": "filler_enabled", "fillerProb": "filler_prob",
        "messageThreadId": "message_thread_id",
    }
    for camel, col in mapping.items():
        val = getattr(req, camel, None)
        if val is not None:
            fields[col] = val

    min_c = fields.get("reply_min_chars", row["reply_min_chars"])
    max_c = fields.get("reply_max_chars", row["reply_max_chars"])
    _validate_chars(min_c, max_c)

    ok = await run_db(topic_repo.update_with_version, topic_id, fields, req.version) if fields else True
    if not ok:
        raise TopicError("话题已被他人修改,请刷新后重试", code=409)

    # 成员变更(覆盖式)
    if req.phones is not None:
        phones = [_normalize(p) for p in req.phones]
        await run_db(topic_member_repo.replace_members, topic_id, phones)

    return await get_topic(topic_id)


async def start_topic(topic_id: int) -> None:
    row = await run_db(topic_repo.find_by_id, topic_id)
    if row is None:
        raise TopicError("话题不存在", code=404)
    await run_db(topic_repo.update_status, topic_id, int(TaskStatus.RUNNING))
    logger.info("话题启动 id=%s", topic_id)


async def stop_topic(topic_id: int) -> None:
    await run_db(topic_repo.update_status, topic_id, int(TaskStatus.STOPPED))
    logger.info("话题停止 id=%s", topic_id)


async def delete_topic(topic_id: int) -> None:
    await run_db(topic_repo.update_status, topic_id, int(TaskStatus.STOPPED))
    await run_db(topic_member_repo.delete_by_topic, topic_id)
    await run_db(topic_repo.delete, topic_id)
    logger.info("话题删除 id=%s(群保留)", topic_id)


async def transfer_owner(topic_id: int, new_owner_phone: str, password: str) -> None:
    """移交群主:用当前 owner 的 client 执行 TransferChatOwnership(需当前 owner 的 2FA 密码)。"""
    row = await run_db(topic_repo.find_by_id, topic_id)
    if row is None:
        raise TopicError("话题不存在", code=404)
    new_owner_phone = _normalize(new_owner_phone)
    owner_client = client_manager.get_ready_client(row["owner_phone"])
    if owner_client is None:
        raise TopicError("当前群主小号未就绪,无法移交")
    new_client = client_manager.get_ready_client(new_owner_phone)
    if new_client is None:
        raise TopicError("新群主小号未就绪")
    try:
        # raw 请求不走 resolve,需自行传 InputChannel / InputUser
        input_channel = await owner_client.get_input_entity(row["chat_id"])
        new_me = await new_client.get_me()
        new_user = types.InputUser(user_id=new_me.id, access_hash=new_me.access_hash or 0)
        await owner_client(_EditCreatorRequest(
            channel=input_channel,
            user_id=new_user,
            password=await _make_password(owner_client, password),
        ))
    except errors.PasswordHashInvalidError as exc:
        raise TopicError("当前群主的 2FA 密码错误") from exc
    except Exception as exc:
        raise TopicError(f"移交群主失败: {td_error.translate(exc)}") from exc
    await run_db(topic_repo.update_owner, topic_id, new_owner_phone)
    logger.info("话题 id=%s 群主移交 %s -> %s", topic_id, row["owner_phone"], new_owner_phone)


async def _make_password(client, password: str):
    from telethon.tl import functions as fns
    pwd = await client(fns.account.GetPasswordRequest())
    from telethon.password import compute_check
    return compute_check(pwd, password)


# ---------- 建群 ----------
async def _create_group(title: str, prompt: str, topic_title: str, phones: List[str]):
    """新建 Forum 超级群,拉成员,并在其下建首个子话题。返回 (owner, chat_id, thread_id, members)。"""
    owner_phone = await _pick_owner(phones)
    owner_client = client_manager.get_ready_client(owner_phone)
    if owner_client is None:
        raise TopicError("群主小号未就绪,无法建群")

    # 1. 建超级群
    created = await owner_client(functions.channels.CreateChannelRequest(
        title=title, about=(prompt or "")[:255], megagroup=True,
    ))
    channel = created.chats[0]
    chat_id = get_peer_id(channel)

    # 2. 开 Forum
    forum_ok = True
    try:
        await owner_client(functions.channels.ToggleForumRequest(channel=channel, enabled=True))
    except Exception as exc:
        forum_ok = False
        logger.warning("开启 Forum 失败(回退 General Topic): %s", exc)

    # 3. 拉其他成员
    members = [owner_phone]
    others = [p for p in phones if p != owner_phone]
    users = []
    for p in others:
        c = client_manager.get_ready_client(p)
        if c is None:
            continue
        try:
            me = await c.get_me()
            users.append(types.InputUser(user_id=me.id, access_hash=me.access_hash or 0))
            members.append(p)
        except Exception as exc:
            logger.warning("解析成员 %s 失败,跳过: %s", p, exc)
    if users:
        try:
            await owner_client(functions.channels.InviteToChannelRequest(channel=channel, users=users))
        except Exception as exc:
            logger.warning("批量拉成员部分失败: %s", exc)

    # 4. 建首个子话题(Forum 开成功才建,否则用 General thread_id=0)
    thread_id = 0
    if forum_ok:
        thread_id = await _create_forum_topic(owner_client, channel, topic_title)

    # 记录在群路由
    for p in members:
        await run_db(account_watch_repo.add, p, chat_id, title)

    return owner_phone, chat_id, thread_id, members


async def _create_forum_topic(owner_client, peer, topic_title: str) -> int:
    """在 Forum 群下新建一个子话题,返回其 thread_id(=该 topic 根消息 id)。失败返回 0。"""
    import random as _r
    try:
        updates = await owner_client(functions.messages.CreateForumTopicRequest(
            peer=peer,
            title=(topic_title or "话题")[:128],
            random_id=_r.randrange(-(2**63), 2**63),
        ))
        thread_id = _extract_message_id(updates)
        if thread_id:
            return thread_id
        logger.warning("建子话题成功但未解析到 thread_id,回退 General")
    except Exception as exc:
        logger.warning("建 Forum 子话题失败(回退 General Topic): %s", exc)
    return 0


def _extract_message_id(updates) -> int:
    """从 CreateForumTopic 返回的 Updates 里提取新建 topic 的根消息 id(即 thread_id)。"""
    for upd in getattr(updates, "updates", []) or []:
        # UpdateMessageID 直接带 id;其它带 message 的更新取 message.id
        mid = getattr(upd, "id", None)
        if mid:
            return int(mid)
        msg = getattr(upd, "message", None)
        if msg is not None and getattr(msg, "id", None):
            return int(msg.id)
    return 0


async def _pick_owner(phones: List[str]) -> str:
    """挑 owner:第一个就绪的号。"""
    for p in phones:
        if client_manager.get_ready_client(p) is not None:
            return p
    raise TopicError("没有就绪的小号可作为群主")


async def _filter_in_group(phones: List[str], chat_id: int):
    from services import group_op_service
    res = await group_op_service.check_member(phones, chat_id)
    in_phones = res["in"]
    skipped = [item["phone"] for item in res["notIn"]]
    return in_phones, skipped


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(w.capitalize() for w in rest)
