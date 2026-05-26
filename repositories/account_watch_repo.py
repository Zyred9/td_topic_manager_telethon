"""t_account_watch 数据访问(小号在群路由表)。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from infra.db import get_connection


def add(phone: str, chat_id: int, chat_title: Optional[str] = None) -> None:
    sql = (
        "INSERT INTO t_account_watch (phone, chat_id, chat_title, joined_at) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE joined_at=VALUES(joined_at), "
        "chat_title=COALESCE(VALUES(chat_title), chat_title)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, chat_id, chat_title, datetime.now()))


def page_by_phone(phone: str, page_no: int, size: int) -> Tuple[List[dict], int]:
    """分页查询某小号已加入的群。返回 (记录, 总数)。"""
    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM t_account_watch WHERE phone=%s", (phone,))
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                "SELECT chat_id, chat_title, joined_at FROM t_account_watch "
                "WHERE phone=%s ORDER BY joined_at DESC, id DESC LIMIT %s OFFSET %s",
                (phone, size, offset),
            )
            rows = cur.fetchall()
    return rows, total


def remove(phone: str, chat_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_account_watch WHERE phone=%s AND chat_id=%s", (phone, chat_id))


def phones_in_chat(chat_id: int) -> List[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM t_account_watch WHERE chat_id=%s", (chat_id,))
            rows = cur.fetchall()
    return [r["phone"] for r in rows]
