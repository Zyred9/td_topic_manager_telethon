"""t_keyword_reply 数据访问(每号独立)。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from infra.db import get_connection


def list_by_phone(phone: str) -> List[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM t_keyword_reply WHERE phone=%s ORDER BY id ASC", (phone,)
            )
            return cur.fetchall()


def insert(phone: str, keyword: str, reply_text: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_keyword_reply (phone, keyword, reply_text, create_time) "
                "VALUES (%s, %s, %s, %s)",
                (phone, keyword, reply_text, datetime.now()),
            )
            return int(cur.lastrowid)


def delete(phone: str, kw_id: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_keyword_reply WHERE id=%s AND phone=%s", (kw_id, phone))
            return int(cur.rowcount)


def find_one(phone: str, kw_id: int) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_keyword_reply WHERE id=%s AND phone=%s", (kw_id, phone))
            return cur.fetchone()
