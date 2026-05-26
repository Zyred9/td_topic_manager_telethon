"""t_account_watch 数据访问(小号在群路由表)。"""

from __future__ import annotations

from datetime import datetime
from typing import List

from infra.db import get_connection


def add(phone: str, chat_id: int) -> None:
    sql = (
        "INSERT INTO t_account_watch (phone, chat_id, joined_at) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE joined_at=VALUES(joined_at)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, chat_id, datetime.now()))


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
