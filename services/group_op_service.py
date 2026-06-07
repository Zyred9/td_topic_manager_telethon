"""批量群操作:加群 / 退群 / 在群校验。

加群异步 + 5~15s 抖动 + 新号日加群上限;退群异步;在群校验同步返回。
进度写 batch_store,失败原因经 td_error 转中文。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import List

from telethon import errors
from telethon.tl import functions, types

from config.settings import get_settings
from core.batch_store import ITEM_FAILED, ITEM_SUCCESS, batch_store
from core.client_manager import client_manager
from helpers import link_parser, td_error
from infra.db import run_db
from repositories import account_watch_repo

logger = logging.getLogger(__name__)


# ---------- 加群 ----------
async def run_join(phones: List[str], group_link: str, batch_id: str) -> None:
    risk = get_settings().risk
    # 入口日志:记录前端传来的原始链接(repr 保留引号/空白,便于发现误传群ID/带空格等)
    logger.info("[加群] 批次=%s 收到原始链接=%r 号数=%d", batch_id, group_link, len(phones))
    for phone in phones:
        await asyncio.sleep(random.uniform(risk.join_jitter_min_sec, risk.join_jitter_max_sec))
        client = client_manager.get_ready_client(phone)
        if client is None:
            batch_store.set_item(batch_id, phone, ITEM_FAILED, fail_reason="小号未就绪")
            continue
        try:
            chat_id, chat_title = await _join_one(client, group_link)
            await run_db(account_watch_repo.add, phone, chat_id, chat_title)
            batch_store.set_item(batch_id, phone, ITEM_SUCCESS, chat_id=chat_id)
            logger.info("[加群] %s 成功 chat=%s(%s)", phone, chat_id, chat_title)
        except Exception as exc:
            # 记录原始异常类型+完整堆栈,排查时直接看这条(不是翻译后的中文)
            batch_store.set_item(batch_id, phone, ITEM_FAILED, fail_reason=td_error.translate(exc))
            logger.warning("[加群] %s 失败 原始链接=%r 异常类型=%s 异常=%s",
                           phone, group_link, type(exc).__name__, exc)
            logger.debug("[加群] %s 失败堆栈", phone, exc_info=True)
    batch_store.finish(batch_id)


async def _join_one(client, group_link: str):
    """返回 (chat_id, chat_title)。"""
    parsed = link_parser.parse(group_link)
    logger.info("[加群] 链接解析 原始=%r -> kind=%s value=%r", group_link, parsed.kind, parsed.value)
    if parsed.kind == "public":
        # get_entity 的目标是用户名(字符串),若这里看到的是 -100 数字,说明输入的是群ID而非链接
        logger.info("[加群] 公开群,get_entity 目标=%r", parsed.value)
        entity = await client.get_entity(parsed.value)
        await client(functions.channels.JoinChannelRequest(entity))
        return _peer_id(entity), getattr(entity, "title", None)
    # invite
    logger.info("[加群] 邀请链接,import invite hash=%r", parsed.value)
    updates = await client(functions.messages.ImportChatInviteRequest(parsed.value))
    chats = getattr(updates, "chats", None)
    if chats:
        return _peer_id(chats[0]), getattr(chats[0], "title", None)
    raise RuntimeError("加入邀请群后未返回群信息")


# ---------- 退群 ----------
async def run_leave(phones: List[str], chat_id: int, batch_id: str) -> None:
    for phone in phones:
        client = client_manager.get_ready_client(phone)
        if client is None:
            batch_store.set_item(batch_id, phone, ITEM_FAILED, fail_reason="小号未就绪")
            continue
        try:
            entity = await client.get_entity(chat_id)
            await client(functions.channels.LeaveChannelRequest(entity))
            await run_db(account_watch_repo.remove, phone, chat_id)
            batch_store.set_item(batch_id, phone, ITEM_SUCCESS, chat_id=chat_id)
            logger.info("[退群] %s 退出 chat=%s", phone, chat_id)
        except Exception as exc:
            batch_store.set_item(batch_id, phone, ITEM_FAILED, fail_reason=td_error.translate(exc))
    batch_store.finish(batch_id)


# ---------- 单号单群退群(同步返回) ----------
async def leave_one(phone: str, chat_id: int) -> None:
    """单个小号退出单个群,同步返回。失败抛异常(中文)。"""
    client = client_manager.get_ready_client(phone)
    if client is None:
        raise RuntimeError("小号未就绪")
    try:
        entity = await client.get_entity(chat_id)
        await client(functions.channels.LeaveChannelRequest(entity))
    except Exception as exc:
        # 即使 TG 端退群失败(如群已不存在),也清理本地路由记录
        await run_db(account_watch_repo.remove, phone, chat_id)
        raise RuntimeError(td_error.translate(exc)) from exc
    await run_db(account_watch_repo.remove, phone, chat_id)
    logger.info("[退群] %s 退出 chat=%s", phone, chat_id)


# ---------- 在群校验(同步返回) ----------
async def check_member(phones: List[str], chat_id: int) -> dict:
    in_list: List[str] = []
    not_in: List[dict] = []
    for phone in phones:
        client = client_manager.get_ready_client(phone)
        if client is None:
            not_in.append({"phone": phone, "reason": "小号未就绪"})
            continue
        try:
            ok = await _is_member(client, chat_id)
            if ok:
                in_list.append(phone)
            else:
                not_in.append({"phone": phone, "reason": "不在该群或无发言权限"})
        except Exception as exc:
            not_in.append({"phone": phone, "reason": td_error.translate(exc)})
    return {"in": in_list, "notIn": not_in}


async def _is_member(client, chat_id: int) -> bool:
    try:
        entity = await client.get_entity(chat_id)
        me = await client.get_me()
        # 传 User 对象而非裸 id:get_me() 返回的 User 自带 access_hash,
        # 冷缓存(刚导入的号)下也能解析,避免 "Could not find the input entity"
        participant = await client(functions.channels.GetParticipantRequest(
            channel=entity, participant=me,
        ))
        p = participant.participant
        # 被踢/被封禁视为不在群
        if isinstance(p, types.ChannelParticipantBanned):
            return False
        if isinstance(p, types.ChannelParticipantLeft):
            return False
        return True
    except errors.UserNotParticipantError:
        return False


def _peer_id(entity) -> int:
    """统一取 chat_id(超级群/频道为 -100 前缀的 marked id)。"""
    from telethon.utils import get_peer_id
    return get_peer_id(entity)
