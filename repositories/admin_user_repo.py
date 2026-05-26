"""t_admin_user 数据访问(同步,经 infra.db.run_db 包线程调用)。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from entities.admin_user import AdminUser
from infra.db import get_connection


def find_by_username(username: str) -> Optional[AdminUser]:
    sql = "SELECT * FROM t_admin_user WHERE username = %s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (username,))
            row = cur.fetchone()
    return AdminUser.from_row(row) if row else None


def find_by_id(user_id: int) -> Optional[AdminUser]:
    sql = "SELECT * FROM t_admin_user WHERE id = %s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            row = cur.fetchone()
    return AdminUser.from_row(row) if row else None


def list_all() -> List[AdminUser]:
    sql = "SELECT * FROM t_admin_user ORDER BY id ASC"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [AdminUser.from_row(r) for r in rows]


def count_super_admin() -> int:
    sql = "SELECT COUNT(*) AS cnt FROM t_admin_user WHERE role = 1"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def insert(username: str, password_hash: str, role: int) -> int:
    now = datetime.now()
    sql = (
        "INSERT INTO t_admin_user (username, password, role, enabled, create_time, update_time) "
        "VALUES (%s, %s, %s, 1, %s, %s)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (username, password_hash, role, now, now))
            return int(cur.lastrowid)


def update_role_enabled(user_id: int, role: Optional[int], enabled: Optional[int]) -> None:
    sets: list[str] = []
    params: list = []
    if role is not None:
        sets.append("role = %s")
        params.append(role)
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(enabled)
    if not sets:
        return
    sets.append("update_time = %s")
    params.append(datetime.now())
    params.append(user_id)
    sql = f"UPDATE t_admin_user SET {', '.join(sets)} WHERE id = %s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))


def update_password(user_id: int, password_hash: str) -> None:
    sql = "UPDATE t_admin_user SET password = %s, update_time = %s WHERE id = %s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (password_hash, datetime.now(), user_id))


def update_last_login(user_id: int) -> None:
    sql = "UPDATE t_admin_user SET last_login = %s WHERE id = %s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (datetime.now(), user_id))


def delete(user_id: int) -> int:
    sql = "DELETE FROM t_admin_user WHERE id = %s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            return int(cur.rowcount)
