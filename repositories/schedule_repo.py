"""t_schedule_task 数据访问(一号一任务)。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from infra.db import get_connection


def find_by_phone(phone: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_schedule_task WHERE phone=%s", (phone,))
            return cur.fetchone()


def list_running() -> List[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_schedule_task WHERE status=1")
            return cur.fetchall()


def upsert_start(phone: str, chat_ids: str, content: str, interval_min: int) -> None:
    now = datetime.now()
    sql = (
        "INSERT INTO t_schedule_task (phone, chat_ids, content, interval_min, status, create_time, update_time) "
        "VALUES (%s, %s, %s, %s, 1, %s, %s) "
        "ON DUPLICATE KEY UPDATE chat_ids=VALUES(chat_ids), content=VALUES(content), "
        "interval_min=VALUES(interval_min), status=1, update_time=VALUES(update_time)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, chat_ids, content, interval_min, now, now))


def update_status(phone: str, status: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_schedule_task SET status=%s, update_time=%s WHERE phone=%s",
                (status, datetime.now(), phone),
            )


def mark_sent(phone: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_schedule_task SET last_sent=%s WHERE phone=%s",
                (datetime.now(), phone),
            )


def stop_all_on_startup() -> None:
    """服务重启不自动恢复任务:全部置停。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE t_schedule_task SET status=0 WHERE status=1")
