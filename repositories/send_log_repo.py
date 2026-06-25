"""t_send_log 数据访问。

发送日志记录所有发送结果(成功/失败),前端按号 + 时间倒序查。
失败时存原始异常类名 + repr(exc),保留 Telethon 给的全部细节。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from infra.db import get_connection


# 内容预览只截前 200 字,与 schema VARCHAR(200) 对齐
_PREVIEW_MAX = 200


def insert(
    phone: str,
    chat_id: int,
    source: str,
    ok: bool,
    err_code: Optional[str] = None,
    err_message: Optional[str] = None,
    content_preview: Optional[str] = None,
) -> None:
    """记一条发送日志。失败时 err_code/err_message 必传,成功时可全 None。"""
    now = datetime.now()
    preview = (content_preview or "")[:_PREVIEW_MAX]
    sql = (
        "INSERT INTO t_send_log (phone, chat_id, source, ok, err_code, err_message, "
        "content_preview, send_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                phone, int(chat_id), source, 1 if ok else 0,
                err_code, err_message, preview, now,
            ))


def phones_with_banned_count_at_least(min_fails: int) -> List[str]:
    """找出累计「被群封」(UserBannedInChannelError)≥ min_fails 条的号,供启动扫描判死。

    口径与实时判死一致:只有 UserBannedInChannelError 才进死号列表。被禁言/限流/超时等
    其余失败不算(它们可能是临时或群级问题,实时也不判死),避免误杀整批好号。
    """
    sql = (
        "SELECT phone FROM t_send_log "
        "WHERE ok = 0 AND err_code = 'UserBannedInChannelError' "
        "GROUP BY phone HAVING COUNT(*) >= %s"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (int(min_fails),))
            rows = cur.fetchall()
    return [r["phone"] for r in rows]


def page_by_phone(
    phone: str, page_no: int, size: int,
    only_failed: bool = False, hours: Optional[int] = None,
) -> Tuple[List[dict], int]:
    """按号分页,可筛只看失败 / 时间窗口(最近 N 小时)。按时间倒序。"""
    where = ["phone = %s"]
    params: list = [phone]
    if only_failed:
        where.append("ok = 0")
    if hours is not None and hours > 0:
        where.append("send_time >= %s")
        params.append(datetime.now() - timedelta(hours=hours))
    clause = " WHERE " + " AND ".join(where)

    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM t_send_log{clause}", tuple(params))
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                f"SELECT * FROM t_send_log{clause} ORDER BY send_time DESC, id DESC LIMIT %s OFFSET %s",
                tuple(params + [size, offset]),
            )
            rows = cur.fetchall()
    return list(rows), total
