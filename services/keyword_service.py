"""关键字回复(每号独立)。

CRUD + 消息监听:对收到的文本做 contains 模糊匹配,命中则引用回复固定文本。
命中策略:多条命中取关键字最长的一条(长度相同取 id 最小,即创建最早);
每条消息最多触发 1 次。发送走 message_sender(source=KEYWORD,配额最高优先级)。

关键字表按 phone 缓存(TTL 默认 300s),增删时失效该号缓存。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from config.constants import SendSource
from core import message_sender, update_router
from infra.db import run_db
from repositories import keyword_repo

logger = logging.getLogger(__name__)

_CACHE_TTL = 300.0
# phone -> (expire_at, [(keyword, reply_text)])
_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"


# ---------- CRUD ----------
async def list_keywords(phone: str) -> List[dict]:
    phone = _normalize(phone)
    rows = await run_db(keyword_repo.list_by_phone, phone)
    return [
        {
            "id": r["id"],
            "phone": r["phone"],
            "keyword": r["keyword"],
            "replyText": r["reply_text"],
            "createTime": r["create_time"].strftime("%Y-%m-%d %H:%M:%S") if r.get("create_time") else None,
        }
        for r in rows
    ]


async def add_keyword(phone: str, keyword: str, reply_text: str) -> int:
    phone = _normalize(phone)
    if not keyword.strip() or not reply_text.strip():
        raise ValueError("关键字和回复文本不能为空")
    kw_id = await run_db(keyword_repo.insert, phone, keyword.strip(), reply_text)
    _cache.pop(phone, None)
    return kw_id


async def delete_keyword(phone: str, kw_id: int) -> None:
    phone = _normalize(phone)
    await run_db(keyword_repo.delete, phone, kw_id)
    _cache.pop(phone, None)


# ---------- 缓存 ----------
async def _get_keywords(phone: str) -> List[Tuple[str, str]]:
    now = time.monotonic()
    cached = _cache.get(phone)
    if cached is not None and cached[0] > now:
        return cached[1]
    rows = await run_db(keyword_repo.list_by_phone, phone)
    # 按关键字长度降序、id 升序排,匹配时第一个命中即最长匹配
    items = sorted(
        ((r["keyword"], r["reply_text"], r["id"]) for r in rows),
        key=lambda x: (-len(x[0]), x[2]),
    )
    result = [(kw, reply) for kw, reply, _ in items]
    _cache[phone] = (now + _CACHE_TTL, result)
    return result


def clear_cache(phone: str) -> None:
    _cache.pop(_normalize(phone), None)


# ---------- 消息监听 ----------
async def on_message(phone: str, event) -> None:
    # 跳过自己发的(避免自触发)
    if getattr(event, "out", False):
        return
    text: Optional[str] = getattr(event, "raw_text", None) or getattr(getattr(event, "message", None), "message", None)
    if not text:
        return

    keywords = await _get_keywords(phone)
    if not keywords:
        return

    for kw, reply in keywords:  # 已按最长优先排序
        if kw in text:
            chat_id = event.chat_id
            msg_id = getattr(event.message, "id", None)
            ok, reason = await message_sender.send_text(
                phone, chat_id, reply, reply_to=msg_id, source=SendSource.KEYWORD,
            )
            if not ok:
                logger.info("[关键字] phone=%s 命中但未发送: %s", phone, reason)
            return  # 每条消息最多触发一次


def register() -> None:
    """注册到 update_router(在 main 启动时调用)。"""
    update_router.register(on_message)
