"""AI 话题运行时:自驱调度 + 应答真人 + owner 失联检测。

触发 A(自驱):周期任务每 5s 扫 running 话题,到点挑成员发言(按概率走附和短句)。
触发 B(应答真人):listener 收到真人发言,按 replyUserProb 介入,走 LLM 生成。

消息上下文缓存:内存按 chat_id 存最近 N 条 "发送者: 内容",自驱/应答共用。
跨功能配额、节流统一走 message_sender + throttle。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from config.constants import CONTEXT_WINDOW, SendSource, TaskStatus
from config.settings import get_settings
from core import message_sender, update_router
from core.client_manager import client_manager
from core.throttle import throttle
from helpers.han_counter import count_han
from infra.db import run_db
from repositories import account_repo, topic_member_repo, topic_repo
from services import ai_chat_engine

logger = logging.getLogger(__name__)


# ---------- 消息上下文缓存 ----------
_LLM_FEED_MIN_HAN = 5  # 小号发言回喂 LLM 的最小汉字门槛,过滤「嗯/哈哈」防短句自激


class MessageCache:
    """按 chat_id 分桶缓存最近消息(普通群,一群一话题)。

    每条带 is_bot 标记:小号自驱/应答产出的句子标 True;真人发言标 False。
    recent_for_llm 把真人 + 长度 >= 5 字的小号发言一起喂回,让 LLM 能接住上下文;
    短小号发言(嗯/哈哈/+1) 不喂,防止「LLM 看到一串短句 → 自己也输出短句」的自激。
    """

    def __init__(self, window: int = CONTEXT_WINDOW) -> None:
        self._window = window
        # 每条 = (is_bot, formatted_line, han_count) — 预存 han 数,recent_for_llm 不重算
        self._data: Dict[int, Deque[tuple[bool, str, int]]] = defaultdict(lambda: deque(maxlen=window))

    def add(self, chat_id: int, sender: str, text: str, is_bot: bool = False) -> None:
        if text:
            self._data[chat_id].append((is_bot, f"{sender}: {text}", count_han(text)))

    def recent(self, chat_id: int) -> List[str]:
        """全部最近消息(真人+小号),仅供调试/前端展示;**不要**直接喂 LLM。"""
        return [line for _, line, _ in self._data.get(chat_id, ())]

    def recent_for_llm(self, chat_id: int) -> List[str]:
        """真人发言 + 小号≥5 字发言喂 LLM。短小号发言过滤掉防短句自激。"""
        return [
            line for is_bot, line, han in self._data.get(chat_id, ())
            if (not is_bot) or han >= _LLM_FEED_MIN_HAN
        ]


_cache = MessageCache()

# 话题自驱节奏:topic_id -> (下次可发言的 monotonic 时间)
_next_speak_at: Dict[int, float] = {}
# 话题每分钟计数:topic_id -> (window_start, count)
_topic_minute: Dict[int, tuple[float, int]] = {}
# owner 离线起始时间:topic_id -> monotonic
_owner_offline_since: Dict[int, float] = {}
# P3 主聊者加权:topic_id -> deque(最近 3 个发言者 phone),刚说过的更可能继续
_last_speakers: Dict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=3))
# P3 群级冷却:topic_id -> (cooldown_until monotonic),期间整群禁言模拟真人作息
_topic_cooldown_until: Dict[int, float] = {}
# P3 近 10 分钟发言数(滑动窗口):topic_id -> deque[发言时间 monotonic]
_topic_recent_emits: Dict[int, Deque[float]] = defaultdict(lambda: deque(maxlen=64))

# P3 冷却触发阈值:10 分钟内发言超过 15 条 → 进入 5-10 分钟冷却
_COOLDOWN_WINDOW_SEC = 600
_COOLDOWN_BURST_THRESHOLD = 15
_COOLDOWN_MIN_SEC = 300
_COOLDOWN_MAX_SEC = 600
# P3 主聊者权重:刚说过的号被选中的相对权重
_LAST_SPEAKER_WEIGHT = 3
# 真人消息级去重:(chat_id, msg_id) -> 进入应答的 monotonic 时间。
# 同一条真人消息会被群内每个在管控小号的 client 各触发一次 on_message,
# 必须保证只有第一次进入应答流程,否则多个号会各自回复造成自答风暴。
_replied_messages: Dict[tuple[int, int], float] = {}
_REPLIED_TTL = 30.0  # 秒,惰性清理

_scheduler_task: Optional[asyncio.Task] = None


def _claim_message(chat_id: int, msg_id: int) -> bool:
    """认领一条真人消息用于应答。第一个认领者返回 True,后续(其他 client 触发)返回 False。"""
    now = time.monotonic()
    # 惰性清理过期项
    if len(_replied_messages) > 512:
        for k in [k for k, t in _replied_messages.items() if now - t > _REPLIED_TTL]:
            _replied_messages.pop(k, None)
    key = (chat_id, msg_id)
    if key in _replied_messages and now - _replied_messages[key] < _REPLIED_TTL:
        return False
    _replied_messages[key] = now
    return True


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"


# ---------- 成员 tg_user_id 解析(区分真人/小号) ----------
async def _member_user_ids(phones: List[str]) -> set[int]:
    ids: set[int] = set()
    for p in phones:
        acc = await run_db(account_repo.find_by_phone, p)
        if acc and acc.tg_user_id:
            ids.add(int(acc.tg_user_id))
    return ids


async def _persona_of(phone: str) -> dict:
    import json
    acc = await run_db(account_repo.find_by_phone, phone)
    if acc and acc.persona_json:
        try:
            return json.loads(acc.persona_json)
        except json.JSONDecodeError:
            return {}
    return {}


def _topic_minute_ok(topic_id: int, max_per_min: int) -> bool:
    now = time.time()
    start, count = _topic_minute.get(topic_id, (now, 0))
    if now - start >= 60:
        return True
    return count < max_per_min


def _topic_incr_minute(topic_id: int) -> None:
    now = time.time()
    start, count = _topic_minute.get(topic_id, (now, 0))
    if now - start >= 60:
        _topic_minute[topic_id] = (now, 1)
    else:
        _topic_minute[topic_id] = (start, count + 1)


# ---------- P3 主聊者加权选择 ----------
def _pick_speaker(topic_id: int, ready: List[str]) -> str:
    """从就绪号里挑发言者:最近说过的号权重 ×3,形成「主聊者 + 附和者」的自然节奏。"""
    recent = _last_speakers[topic_id]
    if not recent:
        return random.choice(ready)
    weights = [_LAST_SPEAKER_WEIGHT if p in recent else 1 for p in ready]
    return random.choices(ready, weights=weights, k=1)[0]


# ---------- P3 群级冷却 ----------
def _topic_in_cooldown(topic_id: int, now: float) -> bool:
    until = _topic_cooldown_until.get(topic_id)
    return until is not None and now < until


def _maybe_enter_cooldown(topic_id: int, now: float) -> bool:
    """若近 10 分钟发言数超阈值,进入 5-10 分钟冷却。返回是否新触发了冷却。"""
    if _topic_in_cooldown(topic_id, now):
        return False
    bucket = _topic_recent_emits[topic_id]
    # 滑动窗口剔除超时项
    cutoff = now - _COOLDOWN_WINDOW_SEC
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _COOLDOWN_BURST_THRESHOLD:
        cooldown = random.uniform(_COOLDOWN_MIN_SEC, _COOLDOWN_MAX_SEC)
        _topic_cooldown_until[topic_id] = now + cooldown
        bucket.clear()  # 冷却后从零开始统计
        logger.info("[话题] id=%s 进入冷却 %.0fs (近 %ds 发言 ≥%d 条)",
                    topic_id, cooldown, _COOLDOWN_WINDOW_SEC, _COOLDOWN_BURST_THRESHOLD)
        return True
    return False


def _record_emit(topic_id: int, now: float) -> None:
    _topic_recent_emits[topic_id].append(now)


# ---------- 触发 A:自驱 ----------
async def _self_drive_tick() -> None:
    topics = await run_db(topic_repo.list_running)
    now = time.monotonic()
    for topic in topics:
        topic_id = topic["id"]
        # owner 失联检测
        await _check_owner(topic)

        # P3 群级冷却:刚聊密了就强制安静一段
        if _topic_in_cooldown(topic_id, now):
            continue

        # 到点判断
        if topic_id in _next_speak_at and now < _next_speak_at[topic_id]:
            continue
        if not _topic_minute_ok(topic_id, topic["max_per_min"]):
            continue

        phones = await run_db(topic_member_repo.phones_of_topic, topic_id)
        ready = [p for p in phones if client_manager.get_ready_client(p) is not None]
        if not ready:
            continue
        # P3 主聊者加权:不再均匀 random.choice
        speaker = _pick_speaker(topic_id, ready)

        # 节流:同号最小间隔 + 每分钟配额
        if not throttle.can_send_now(speaker) or not throttle.minute_quota_available(speaker):
            continue

        await _emit_self(topic, speaker)

        # 安排下次发言时间
        delay = random.uniform(topic["speak_min_sec"], topic["speak_max_sec"])
        _next_speak_at[topic_id] = now + delay
        # P3 触发冷却检查(发完才记一次,失败的不算)
        _maybe_enter_cooldown(topic_id, now)


async def _emit_self(topic: dict, speaker: str) -> None:
    chat_id = topic["chat_id"]
    persona = await _persona_of(speaker)
    name = persona.get("name") or speaker
    # 附和短句(仅自驱)
    if topic["filler_enabled"] == 1 and random.randint(0, 99) < topic["filler_prob"]:
        text = ai_chat_engine.pick_filler(persona)
        logger.debug("[话题] filler phone=%s topic_id=%s", speaker, topic["id"])
    else:
        text = await ai_chat_engine.generate_reply(
            persona, topic["topic_prompt"], _cache.recent_for_llm(chat_id),
            reply_target=None, topic_min=topic["reply_min_chars"], topic_max=topic["reply_max_chars"],
        )
        logger.debug("[话题] LLM phone=%s topic_id=%s len=%d", speaker, topic["id"], len(text))
    if not text:
        return
    ok, _ = await message_sender.send_text(speaker, chat_id, text, source=SendSource.AI_SELF)
    if ok:
        _topic_incr_minute(topic["id"])
        # P3 记录发言者(主聊者加权用)+ 群级近期发言数(冷却用)
        _last_speakers[topic["id"]].append(speaker)
        _record_emit(topic["id"], time.monotonic())
        # is_bot=True: 自驱产物入缓存,≥5 字部分会被 recent_for_llm 回喂给 LLM 接住上下文
        _cache.add(chat_id, name, text, is_bot=True)


async def _check_owner(topic: dict) -> None:
    owner = topic["owner_phone"]
    threshold = get_settings().risk.owner_offline_threshold_sec
    if client_manager.get_ready_client(owner) is not None:
        _owner_offline_since.pop(topic["id"], None)
        return
    since = _owner_offline_since.setdefault(topic["id"], time.monotonic())
    if time.monotonic() - since > threshold:
        logger.warning("[话题] id=%s 群主 %s 离线超 %ds,自动停话题,需人工介入移交群主",
                       topic["id"], owner, threshold)
        await run_db(topic_repo.update_status, topic["id"], int(TaskStatus.STOPPED))
        _owner_offline_since.pop(topic["id"], None)


# ---------- 触发 B:应答真人 ----------
async def on_message(phone: str, event) -> None:
    """作为 update_router listener:记录上下文 + 按概率应答真人。

    注意:每条群消息会被群里所有在管控小号各触发一次本函数(各自的 client 都收到)。
    用 reply_lock 保证同一条真人发言在多个候选小号里只回一次。
    """
    chat_id = event.chat_id
    if chat_id is None:
        return
    # 找该群的 running 话题(一群一话题)
    topic = await _find_running_topic(chat_id)
    if topic is None:
        return

    sender_id = _sender_id(event)
    # 匿名管理员 / 频道消息 / 服务消息(进群通知等)sender_id 为 None,不应答
    if sender_id is None:
        return

    text = getattr(event, "raw_text", None) or ""
    if not text:
        return

    phones = await run_db(topic_member_repo.phones_of_topic, topic["id"])
    member_ids = await _member_user_ids(phones)

    # 小号自己发的:标 is_bot=True 入缓存(用于去重/调试),但不喂回 LLM,不应答。
    # 每个在管控小号的 client 都会收到这条;用 out 仅由发送方记录一次,避免重复入缓存。
    if sender_id in member_ids:
        if getattr(event, "out", False):
            _cache.add(chat_id, _member_name(member_ids, sender_id, event), text, is_bot=True)
        return

    # 真人发言:消息级去重(同一条消息会被多个 client 触发)
    msg_id = getattr(event.message, "id", None)
    if msg_id is None or not _claim_message(chat_id, msg_id):
        return

    # 记上下文(用真实发送者名),真人发言会被喂回 LLM
    sender_name = getattr(getattr(event, "sender", None), "first_name", None) or "用户"
    _cache.add(chat_id, sender_name, text, is_bot=False)

    # 概率应答
    if random.randint(0, 99) >= topic["reply_user_prob"]:
        return

    ready = [p for p in phones if client_manager.get_ready_client(p) is not None]
    if not ready:
        return
    responder = random.choice(ready)

    # 节流:每分钟配额(消息级去重已保证不重复应答)
    if not throttle.minute_quota_available(responder):
        return

    persona = await _persona_of(responder)
    reply = await ai_chat_engine.generate_reply(
        persona, topic["topic_prompt"], _cache.recent_for_llm(chat_id),
        reply_target=text, topic_min=topic["reply_min_chars"], topic_max=topic["reply_max_chars"],
    )
    if not reply:
        return
    ok, _ = await message_sender.send_text(
        responder, chat_id, reply, reply_to=msg_id, source=SendSource.AI_REPLY,
    )
    if ok:
        _topic_incr_minute(topic["id"])
        # 应答产物也是 bot 发言,标 is_bot=True
        _cache.add(chat_id, persona.get("name") or responder, reply, is_bot=True)


async def _find_running_topic(chat_id: int) -> Optional[dict]:
    """找该群的 running 话题(普通群,一群一话题)。"""
    topics = await run_db(topic_repo.list_running)
    for t in topics:
        if t["chat_id"] == chat_id:
            return t
    return None


def _member_name(member_ids: set, sender_id: int, event) -> str:
    return getattr(getattr(event, "sender", None), "first_name", None) or str(sender_id)


def _sender_id(event) -> Optional[int]:
    sid = getattr(event, "sender_id", None)
    return int(sid) if sid else None


# ---------- 调度器生命周期 ----------
async def _scheduler_loop() -> None:
    while True:
        try:
            await _self_drive_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("话题自驱调度异常")
        await asyncio.sleep(5)


def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("话题自驱调度器已启动")


async def stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except (asyncio.CancelledError, Exception):
            pass


def register() -> None:
    update_router.register(on_message)


def remove_member_phone(phone: str) -> None:
    """删除小号时调用:从节流相关内存里清理(成员表删除在级联事务里做)。"""
    throttle.clear_phone(_normalize(phone))


async def stop_all_on_startup() -> None:
    await run_db(topic_repo.stop_all_on_startup)
