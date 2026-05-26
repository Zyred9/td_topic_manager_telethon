"""私聊消息记录:作为 update_router listener,记录小号收到的一对一私聊。

只记:私聊(is_private) + 非自己发出(not out)。群消息、自己发的不记。
媒体:图片/视频/文件下载到 data/media/(超 DM_MEDIA_MAX_BYTES 只记元信息);
贴纸只记 '[表情]' 不下载。文本/caption 存 content。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config.constants import DM_MEDIA_MAX_BYTES
from config.settings import get_settings
from core import update_router
from core.client_manager import client_manager
from infra.db import run_db
from repositories import dm_repo

logger = logging.getLogger(__name__)


def _normalize(phone: str) -> str:
    p = (phone or "").strip()
    return p if p.startswith("+") else f"+{p}"


def _classify(message) -> str:
    """判断消息类型。"""
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "video", None) is not None or getattr(message, "video_note", None) is not None:
        return "video"
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "document", None) is not None or getattr(message, "file", None) is not None:
        return "file"
    if getattr(message, "message", None):
        return "text"
    return "other"


async def on_message(phone: str, event) -> None:
    """update_router listener:记录收到的私聊。"""
    # 只记一对一私聊,且不是自己发出的
    if not getattr(event, "is_private", False):
        return
    if getattr(event, "out", False):
        return

    phone = _normalize(phone)
    message = event.message
    sender_id = getattr(event, "sender_id", None)
    if sender_id is None:
        return

    now = datetime.now()
    msg_time = getattr(message, "date", None) or now
    # date 是带时区的 UTC,落库去 tzinfo(与其它表 DATETIME 一致用本地朴素时间)
    try:
        msg_time = msg_time.replace(tzinfo=None) if getattr(msg_time, "tzinfo", None) else msg_time
    except Exception:
        msg_time = now

    msg_type = _classify(message)
    text = getattr(message, "message", None) or None  # 文本或媒体 caption
    media_path: Optional[str] = None
    media_size: Optional[int] = None
    media_note: Optional[str] = None

    # 媒体下载(贴纸不下载只记 [表情])
    if msg_type == "sticker":
        text = text or "[表情]"
    elif msg_type in ("photo", "video", "file", "voice"):
        media_path, media_size, media_note = await _maybe_download(phone, sender_id, message, msg_type)

    # 发送者资料
    sender = getattr(event, "sender", None)
    username = getattr(sender, "username", None)
    first_name = getattr(sender, "first_name", None)
    last_name = getattr(sender, "last_name", None)

    try:
        await run_db(dm_repo.upsert_peer, phone, int(sender_id), username, first_name, last_name, msg_time)
        await run_db(
            dm_repo.insert_message, phone, int(sender_id),
            getattr(message, "id", None), msg_type, text,
            media_path, media_size, media_note, msg_time,
        )
    except Exception:
        logger.exception("[私聊] 落库失败 phone=%s peer=%s", phone, sender_id)


async def _maybe_download(phone: str, sender_id: int, message, msg_type: str):
    """按大小限制下载媒体。返回 (相对路径, 大小, 未下载说明)。"""
    size = getattr(getattr(message, "file", None), "size", None)
    if size is not None and size > DM_MEDIA_MAX_BYTES:
        mb = DM_MEDIA_MAX_BYTES // (1024 * 1024)
        return None, size, f"超过 {mb}MB 未下载({msg_type})"

    client = client_manager.get_ready_client(phone)
    if client is None:
        return None, size, "小号未就绪,未下载"

    settings = get_settings()
    # 每号一个子目录:media/{phone}/{tg_msg_id}_<原文件名>
    sub = settings.media_dir / phone.lstrip("+")
    sub.mkdir(parents=True, exist_ok=True)
    try:
        saved = await client.download_media(message, file=str(sub))
        if not saved:
            return None, size, "下载失败"
        from pathlib import Path
        rel = f"{phone.lstrip('+')}/{Path(saved).name}"
        actual = Path(saved).stat().st_size if Path(saved).exists() else size
        return rel, actual, None
    except Exception as exc:
        logger.warning("[私聊] 媒体下载失败 phone=%s: %s", phone, exc)
        return None, size, "下载异常"


# ---------- 查询(供前端三层展示) ----------
def _peer_name(first: Optional[str], last: Optional[str], username: Optional[str], peer_id: int) -> str:
    name = " ".join(x for x in (first, last) if x).strip()
    if name:
        return name
    if username:
        return f"@{username}"
    return str(peer_id)


async def list_accounts_with_dm() -> list:
    """有私聊记录的小号列表(含 TG 名字),供左侧第一层。"""
    from repositories import account_repo
    rows = await run_db(dm_repo.list_phones_with_dm)
    result = []
    for r in rows:
        acc = await run_db(account_repo.find_by_phone, r["phone"])
        tg_name = ""
        if acc:
            tg_name = " ".join(x for x in (acc.tg_first_name, acc.tg_last_name) if x).strip()
        result.append({
            "phone": r["phone"],
            "tgName": tg_name or None,
            "tgUsername": acc.tg_username if acc else None,
            "tgUserId": acc.tg_user_id if acc else None,
            "peerCount": int(r["peer_count"]),
            "msgTotal": int(r["msg_total"]),
        })
    return result


async def list_peers(phone: str) -> list:
    """某小号的私聊对象列表(第二层)。"""
    phone = _normalize(phone)
    rows = await run_db(dm_repo.list_peers, phone)
    return [
        {
            "peerUserId": r["peer_user_id"],
            "username": r["username"],
            "firstName": r["first_name"],
            "lastName": r["last_name"],
            "displayName": _peer_name(r["first_name"], r["last_name"], r["username"], r["peer_user_id"]),
            "lastMsgTime": r["last_msg_time"].strftime("%Y-%m-%d %H:%M:%S") if r.get("last_msg_time") else None,
            "msgCount": int(r["msg_count"]),
        }
        for r in rows
    ]


async def list_messages(phone: str, peer_user_id: int, page_no: int, size: int) -> dict:
    """某小号与某对象的消息分页(第三层)。media_path 拼成 web 可访问的 url。"""
    phone = _normalize(phone)
    rows, total = await run_db(dm_repo.page_messages, phone, peer_user_id, page_no, size)
    records = [
        {
            "tgMsgId": r["tg_msg_id"],
            "msgType": r["msg_type"],
            "content": r["content"],
            # 前端经 /api/static/media/<path> 预览(Nginx/StaticFiles 服务)
            "mediaUrl": f"static/media/{r['media_path']}" if r.get("media_path") else None,
            "mediaSize": r["media_size"],
            "mediaNote": r["media_note"],
            "msgTime": r["msg_time"].strftime("%Y-%m-%d %H:%M:%S") if r.get("msg_time") else None,
        }
        for r in rows
    ]
    return {"page": page_no, "size": size, "total": total, "records": records}


def register() -> None:
    update_router.register(on_message)
