"""t_ai_topic_member 数据访问。"""

from __future__ import annotations

from typing import List

from infra.db import get_connection


def phones_of_topic(topic_id: int) -> List[str]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM t_ai_topic_member WHERE topic_id=%s", (topic_id,))
            return [r["phone"] for r in cur.fetchall()]


def count_of_topic(topic_id: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM t_ai_topic_member WHERE topic_id=%s", (topic_id,))
            return int(cur.fetchone()["cnt"])


def topic_count_of_phone(phone: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM t_ai_topic_member WHERE phone=%s", (phone,))
            return int(cur.fetchone()["cnt"])


def replace_members(topic_id: int, phones: List[str]) -> None:
    """覆盖式设置成员(编辑话题成员用)。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_ai_topic_member WHERE topic_id=%s", (topic_id,))
            for phone in phones:
                cur.execute(
                    "INSERT IGNORE INTO t_ai_topic_member (topic_id, phone) VALUES (%s, %s)",
                    (topic_id, phone),
                )


def add_members(topic_id: int, phones: List[str]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for phone in phones:
                cur.execute(
                    "INSERT IGNORE INTO t_ai_topic_member (topic_id, phone) VALUES (%s, %s)",
                    (topic_id, phone),
                )


def delete_by_topic(topic_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_ai_topic_member WHERE topic_id=%s", (topic_id,))
