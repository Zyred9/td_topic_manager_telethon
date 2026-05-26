"""t_account 数据访问。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from entities.account import Account
from infra.db import get_connection


def find_by_phone(phone: str) -> Optional[Account]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_account WHERE phone = %s", (phone,))
            row = cur.fetchone()
    return Account.from_row(row) if row else None


def list_all() -> List[Account]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_account ORDER BY id ASC")
            rows = cur.fetchall()
    return [Account.from_row(r) for r in rows]


def find_by_status(status: int) -> List[Account]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_account WHERE status = %s", (status,))
            rows = cur.fetchall()
    return [Account.from_row(r) for r in rows]


def find_by_statuses(statuses: List[int]) -> List[Account]:
    """按多个状态查询(启动全起用:已登录 + 需重新登录都试连)。"""
    if not statuses:
        return []
    placeholders = ",".join(["%s"] * len(statuses))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM t_account WHERE status IN ({placeholders})", tuple(statuses))
            rows = cur.fetchall()
    return [Account.from_row(r) for r in rows]


def page(
    page_no: int, size: int, keyword: Optional[str], status: Optional[int]
) -> Tuple[List[Account], int]:
    """分页 + 搜索(电话/用户名模糊、状态精确)。返回 (记录, 总数)。"""
    where: list[str] = []
    params: list = []
    if keyword:
        where.append("(phone LIKE %s OR tg_username LIKE %s OR tg_first_name LIKE %s)")
        like = f"%{keyword}%"
        params += [like, like, like]
    if status is not None:
        where.append("status = %s")
        params.append(status)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM t_account{clause}", tuple(params))
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                f"SELECT * FROM t_account{clause} ORDER BY login_time DESC, id DESC LIMIT %s OFFSET %s",
                tuple(params + [size, offset]),
            )
            rows = cur.fetchall()
    return [Account.from_row(r) for r in rows], total


def upsert_protocol(
    phone: str,
    session_path: str,
    api_id: Optional[int],
    api_hash: Optional[str],
    device_model: Optional[str],
    app_version: Optional[str],
    lang_pack: Optional[str],
    two_fa: Optional[str],
) -> None:
    """协议号导入 upsert(import_type=2)。冲突时更新凭证与 session_path。"""
    now = datetime.now()
    sql = (
        "INSERT INTO t_account (phone, import_type, session_path, api_id, api_hash, "
        "device_model, app_version, lang_pack, two_fa, status, create_time, update_time) "
        "VALUES (%s, 2, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s) "
        "ON DUPLICATE KEY UPDATE session_path=VALUES(session_path), api_id=VALUES(api_id), "
        "api_hash=VALUES(api_hash), device_model=VALUES(device_model), "
        "app_version=VALUES(app_version), lang_pack=VALUES(lang_pack), "
        "two_fa=VALUES(two_fa), status=1, update_time=VALUES(update_time)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, session_path, api_id, api_hash, device_model,
                              app_version, lang_pack, two_fa, now, now))


def upsert_phone(phone: str, session_path: str) -> None:
    """手机号登录 upsert(import_type=1,用全局凭证,api 字段留空)。"""
    now = datetime.now()
    sql = (
        "INSERT INTO t_account (phone, import_type, session_path, status, create_time, update_time) "
        "VALUES (%s, 1, %s, 1, %s, %s) "
        "ON DUPLICATE KEY UPDATE session_path=VALUES(session_path), import_type=1, "
        "status=1, update_time=VALUES(update_time)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, session_path, now, now))


def update_status(phone: str, status: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_account SET status=%s, update_time=%s WHERE phone=%s",
                (status, datetime.now(), phone),
            )


def mark_logged_in(phone: str, tg_user_id: Optional[int], first_name: Optional[str],
                   last_name: Optional[str], username: Optional[str]) -> None:
    """登录成功:回填 TG 信息 + status=3 + login_time。"""
    now = datetime.now()
    sql = (
        "UPDATE t_account SET status=3, tg_user_id=%s, tg_first_name=%s, tg_last_name=%s, "
        "tg_username=%s, login_time=%s, update_time=%s WHERE phone=%s"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (tg_user_id, first_name, last_name, username, now, now, phone))


def update_profile(phone: str, first_name: Optional[str], last_name: Optional[str],
                   username: Optional[str], bio: Optional[str]) -> None:
    sets: list[str] = []
    params: list = []
    for col, val in (("tg_first_name", first_name), ("tg_last_name", last_name),
                     ("tg_username", username), ("bio", bio)):
        if val is not None:
            sets.append(f"{col}=%s")
            params.append(val)
    if not sets:
        return
    sets.append("update_time=%s")
    params += [datetime.now(), phone]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE t_account SET {', '.join(sets)} WHERE phone=%s", tuple(params))


def update_avatar(phone: str, avatar_path: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_account SET avatar_path=%s, update_time=%s WHERE phone=%s",
                (avatar_path, datetime.now(), phone),
            )


def update_persona(phone: str, persona_json: Optional[str]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_account SET persona_json=%s, update_time=%s WHERE phone=%s",
                (persona_json, datetime.now(), phone),
            )


def reset_all_status_on_startup() -> None:
    """服务启动:上次运行态(初始化中1/等待码2)统一置离线(4),
    已登录(3)的号保持 3 交给 ClientManager 全起后重连校验回写,
    不动 NEED_RELOGIN(5)/WAIT_2FA(6)。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE t_account SET status=4 WHERE status IN (1,2)")


def delete_by_phone(phone: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_account WHERE phone=%s", (phone,))
            return int(cur.rowcount)
