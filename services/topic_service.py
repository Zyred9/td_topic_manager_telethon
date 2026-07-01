"""AI 话题:CRUD + 在群校验 + 乐观锁 + 移交群主。

不建群:话题只接入已有的普通群(创建时填群 ID,校验小号在群、剔除不在群的)。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from telethon import errors
from telethon.tl import types
from telethon.tl.tlobject import TLRequest

from config.constants import TaskStatus
from core.client_manager import client_manager
from helpers import td_error
from helpers.dead_account import is_dead_error, mark_dead_and_remove
from infra.db import run_db
from repositories import account_watch_repo, topic_member_repo, topic_repo
from services import ai_chat_engine

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


# 创建话题时可由前端覆盖的字段默认值。message_thread_id 自 aca8422
# 移除 Forum 子话题逻辑后恒为 0,DB 列保留但不再走 req 取值。
#
# 2026-05-28 调优: 之前 10~45s + 每分钟 12 条 + filler_prob 10 仍偏密,
# 改为 30~90s + 每分钟 10 条, 配合 FILLER_POOL 自然化减少刷屏感。
# 已有话题不会被自动改写, 运营在「话题编辑」里手动改才生效。
_DEFAULTS = {
    "speak_min_sec": 30, "speak_max_sec": 90,
    "reply_user_prob": 30, "max_per_min": 10, "reply_min_chars": 15,
    "reply_max_chars": 40, "filler_enabled": 1, "filler_prob": 15,
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

    # 接入已有普通群:必须填群 ID,校验小号在群,剔除不在群的
    if req.chatId is None:
        raise TopicError("请填写群 ID")
    in_phones, skipped = await _filter_in_group(phones, req.chatId)
    if not in_phones:
        raise TopicError("所选小号均不在该群,无法创建话题")
    chat_id = req.chatId
    owner_phone = await _pick_owner(in_phones)
    members = in_phones
    for p in members:
        await run_db(account_watch_repo.add, p, chat_id)

    # topic_prompt 留空 → 调 LLM 按话题名自动生成完整场景;失败抛业务异常给前端
    topic_prompt = (req.topicPrompt or "").strip()
    if not topic_prompt:
        if not req.topicName or not req.topicName.strip():
            raise TopicError("话题名不能为空")
        try:
            topic_prompt = await ai_chat_engine.generate_topic_prompt(req.topicName)
            logger.info("自动生成场景 topic_name=%s len=%d", req.topicName, len(topic_prompt))
        except Exception as exc:
            raise TopicError(f"自动生成场景失败,请手填或重试: {exc}") from exc

    data = {
        "topic_name": req.topicName, "topic_prompt": topic_prompt,
        "owner_phone": owner_phone, "chat_id": chat_id, "message_thread_id": 0,
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
        # 终态死号异常 → 判死。绝大多数 RPC 由 owner_client 发起(get_input_entity /
        # EditCreator / make_password),仅 get_me 走 new_client;无法从单个异常精确定位
        # 来源,故只对主操作方 owner 判死,避免盲标两个号误杀好的新群主号。
        if is_dead_error(exc):
            await mark_dead_and_remove(row["owner_phone"], exc)
        raise TopicError(f"移交群主失败: {td_error.translate(exc)}") from exc
    await run_db(topic_repo.update_owner, topic_id, new_owner_phone)
    logger.info("话题 id=%s 群主移交 %s -> %s", topic_id, row["owner_phone"], new_owner_phone)


async def _make_password(client, password: str):
    from telethon.tl import functions as fns
    pwd = await client(fns.account.GetPasswordRequest())
    from telethon.password import compute_check
    return compute_check(pwd, password)


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
