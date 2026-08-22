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


def page_schedule_failures(page_no: int, size: int) -> Tuple[List[dict], int]:
    """定时发送失败去重分页:source=SCHEDULE 且 ok=0,按 phone+chat_id 去重保留最新一条,时间倒序。

    id 自增与 send_time 同序,MAX(id) 即最新失败记录。LEFT JOIN t_account 回填 TG 昵称。
    """
    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size

    count_sql = (
        "SELECT COUNT(*) AS cnt FROM ("
        "SELECT phone, chat_id FROM t_send_log "
        "WHERE source='SCHEDULE' AND ok=0 GROUP BY phone, chat_id"
        ") t"
    )
    data_sql = (
        "SELECT l.*, a.tg_first_name, a.tg_last_name, a.tg_username "
        "FROM t_send_log l "
        "JOIN ("
        "  SELECT phone, chat_id, MAX(id) AS max_id FROM t_send_log "
        "  WHERE source='SCHEDULE' AND ok=0 GROUP BY phone, chat_id"
        ") d ON l.id = d.max_id "
        "LEFT JOIN t_account a ON a.phone = l.phone "
        "ORDER BY l.send_time DESC, l.id DESC LIMIT %s OFFSET %s"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(count_sql)
            total = int(cur.fetchone()["cnt"])
            cur.execute(data_sql, (size, offset))
            rows = cur.fetchall()
    return list(rows), total
