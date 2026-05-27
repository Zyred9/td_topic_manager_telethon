"""私聊记录数据访问(t_dm_peer 私聊对象 + t_dm_message 消息)。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from infra.db import get_connection


# ---------- 私聊对象 ----------
def upsert_peer(phone: str, peer_user_id: int, username: Optional[str],
                first_name: Optional[str], last_name: Optional[str], msg_time: datetime) -> None:
    """收到一条私聊时 upsert 对象:更新资料、最近时间、消息计数 +1。"""
    now = datetime.now()
    sql = (
        "INSERT INTO t_dm_peer (phone, peer_user_id, username, first_name, last_name, "
        "last_msg_time, msg_count, create_time) VALUES (%s, %s, %s, %s, %s, %s, 1, %s) "
        "ON DUPLICATE KEY UPDATE "
        "username=VALUES(username), first_name=VALUES(first_name), last_name=VALUES(last_name), "
        "last_msg_time=VALUES(last_msg_time), msg_count=msg_count+1"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, peer_user_id, username, first_name, last_name, msg_time, now))


def list_phones_with_dm() -> List[dict]:
    """有私聊记录的小号 + 各自对象数/消息数(供左侧小号列表)。"""
    sql = (
        "SELECT phone, COUNT(*) AS peer_count, COALESCE(SUM(msg_count),0) AS msg_total "
        "FROM t_dm_peer GROUP BY phone ORDER BY MAX(last_msg_time) DESC"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def list_peers(phone: str) -> List[dict]:
    """某小号的所有私聊对象,按最近消息时间倒序。"""
    sql = (
        "SELECT peer_user_id, username, first_name, last_name, last_msg_time, msg_count "
        "FROM t_dm_peer WHERE phone=%s ORDER BY last_msg_time DESC"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone,))
            return cur.fetchall()


# ---------- 消息 ----------
def insert_message(phone: str, peer_user_id: int, tg_msg_id: Optional[int], msg_type: str,
                   content: Optional[str], media_path: Optional[str], media_size: Optional[int],
                   media_note: Optional[str], msg_time: datetime) -> int:
    now = datetime.now()
    sql = (
        "INSERT INTO t_dm_message (phone, peer_user_id, tg_msg_id, msg_type, content, "
        "media_path, media_size, media_note, msg_time, create_time) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, peer_user_id, tg_msg_id, msg_type, content,
                              media_path, media_size, media_note, msg_time, now))
            return int(cur.lastrowid)


def page_messages(phone: str, peer_user_id: int, page_no: int, size: int) -> Tuple[List[dict], int]:
    """某小号与某对象的消息分页,按时间倒序(最新在前)。"""
    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM t_dm_message WHERE phone=%s AND peer_user_id=%s",
                (phone, peer_user_id),
            )
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                "SELECT id, tg_msg_id, msg_type, content, media_path, media_size, media_note, msg_time "
                "FROM t_dm_message WHERE phone=%s AND peer_user_id=%s "
                "ORDER BY msg_time DESC, id DESC LIMIT %s OFFSET %s",
                (phone, peer_user_id, size, offset),
            )
            rows = cur.fetchall()
    return rows, total


# ---------- 删除(只删后台记录,不动 Telegram) ----------
def delete_messages_by_ids(ids: List[int]) -> Tuple[int, List[str]]:
    """按消息主键批量删除。返回 (受影响行数, 被删消息的 media_path 列表)。

    删除后重算受影响 peer 的 msg_count/last_msg_time,保持计数与列表一致。
    """
    if not ids:
        return 0, []
    placeholders = ",".join(["%s"] * len(ids))
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 先取待删消息的 media_path 与归属 peer(供清文件 + 重算计数)
            cur.execute(
                f"SELECT media_path, phone, peer_user_id FROM t_dm_message WHERE id IN ({placeholders})",
                tuple(ids),
            )
            rows = cur.fetchall()
            media_paths = [r["media_path"] for r in rows if r.get("media_path")]
            peers = {(r["phone"], r["peer_user_id"]) for r in rows}
            cur.execute(f"DELETE FROM t_dm_message WHERE id IN ({placeholders})", tuple(ids))
            affected = int(cur.rowcount)
            for phone, peer_user_id in peers:
                _recount_peer(cur, phone, peer_user_id)
    return affected, media_paths


def delete_peer(phone: str, peer_user_id: int) -> Tuple[int, List[str]]:
    """删除某对象的全部消息 + 对象本身。返回 (删除消息数, media_path 列表)。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT media_path FROM t_dm_message WHERE phone=%s AND peer_user_id=%s",
                (phone, peer_user_id),
            )
            media_paths = [r["media_path"] for r in cur.fetchall() if r.get("media_path")]
            cur.execute(
                "DELETE FROM t_dm_message WHERE phone=%s AND peer_user_id=%s",
                (phone, peer_user_id),
            )
            affected = int(cur.rowcount)
            cur.execute(
                "DELETE FROM t_dm_peer WHERE phone=%s AND peer_user_id=%s",
                (phone, peer_user_id),
            )
    return affected, media_paths


def delete_all_by_phone(phone: str) -> Tuple[int, List[str]]:
    """删除某小号的全部私聊(所有消息 + 所有对象)。返回 (删除消息数, media_path 列表)。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT media_path FROM t_dm_message WHERE phone=%s", (phone,))
            media_paths = [r["media_path"] for r in cur.fetchall() if r.get("media_path")]
            cur.execute("DELETE FROM t_dm_message WHERE phone=%s", (phone,))
            affected = int(cur.rowcount)
            cur.execute("DELETE FROM t_dm_peer WHERE phone=%s", (phone,))
    return affected, media_paths


def _recount_peer(cur, phone: str, peer_user_id: int) -> None:
    """重算某 peer 的 msg_count/last_msg_time;无消息则删除该 peer 行。"""
    cur.execute(
        "SELECT COUNT(*) AS cnt, MAX(msg_time) AS last_time "
        "FROM t_dm_message WHERE phone=%s AND peer_user_id=%s",
        (phone, peer_user_id),
    )
    row = cur.fetchone()
    cnt = int(row["cnt"]) if row else 0
    if cnt == 0:
        cur.execute(
            "DELETE FROM t_dm_peer WHERE phone=%s AND peer_user_id=%s",
            (phone, peer_user_id),
        )
        return
    cur.execute(
        "UPDATE t_dm_peer SET msg_count=%s, last_msg_time=%s WHERE phone=%s AND peer_user_id=%s",
        (cnt, row["last_time"], phone, peer_user_id),
    )
