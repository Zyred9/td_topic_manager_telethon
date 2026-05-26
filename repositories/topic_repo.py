"""t_ai_topic 数据访问。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from infra.db import get_connection

_FIELDS = (
    "topic_name, topic_prompt, owner_phone, chat_id, message_thread_id, "
    "speak_min_sec, speak_max_sec, reply_user_prob, max_per_min, "
    "reply_min_chars, reply_max_chars, filler_enabled, filler_prob, status, version"
)


def find_by_id(topic_id: int) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_ai_topic WHERE id=%s", (topic_id,))
            return cur.fetchone()


def list_all() -> List[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_ai_topic ORDER BY id DESC")
            return cur.fetchall()


def list_running() -> List[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_ai_topic WHERE status=1")
            return cur.fetchall()


def insert(data: dict) -> int:
    now = datetime.now()
    sql = (
        "INSERT INTO t_ai_topic (topic_name, topic_prompt, owner_phone, chat_id, "
        "message_thread_id, speak_min_sec, speak_max_sec, reply_user_prob, max_per_min, "
        "reply_min_chars, reply_max_chars, filler_enabled, filler_prob, status, version, "
        "create_time, update_time) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                data["topic_name"], data["topic_prompt"], data["owner_phone"], data["chat_id"],
                data["message_thread_id"], data["speak_min_sec"], data["speak_max_sec"],
                data["reply_user_prob"], data["max_per_min"], data["reply_min_chars"],
                data["reply_max_chars"], data["filler_enabled"], data["filler_prob"], now, now,
            ))
            return int(cur.lastrowid)


def update_with_version(topic_id: int, fields: dict, expected_version: int) -> bool:
    """乐观锁更新:version 匹配才更新且 version+1。返回是否更新成功。"""
    sets = [f"{k}=%s" for k in fields]
    sets.append("version=version+1")
    sets.append("update_time=%s")
    params = list(fields.values()) + [datetime.now(), topic_id, expected_version]
    sql = f"UPDATE t_ai_topic SET {', '.join(sets)} WHERE id=%s AND version=%s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.rowcount > 0


def update_status(topic_id: int, status: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_ai_topic SET status=%s, update_time=%s WHERE id=%s",
                (status, datetime.now(), topic_id),
            )


def update_owner(topic_id: int, new_owner_phone: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_ai_topic SET owner_phone=%s, update_time=%s WHERE id=%s",
                (new_owner_phone, datetime.now(), topic_id),
            )


def delete(topic_id: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_ai_topic WHERE id=%s", (topic_id,))
            return int(cur.rowcount)


def stop_all_on_startup() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE t_ai_topic SET status=0 WHERE status=1")


def list_forum_chats() -> List[dict]:
    """返回平台已有话题中用过的 Forum 群(message_thread_id>0),按 chat_id 去重,带群名。
    群名取该群任一 watch 记录的 chat_title。"""
    sql = (
        "SELECT t.chat_id AS chat_id, MAX(w.chat_title) AS chat_title "
        "FROM t_ai_topic t "
        "LEFT JOIN t_account_watch w ON w.chat_id = t.chat_id "
        "WHERE t.message_thread_id > 0 "
        "GROUP BY t.chat_id"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
