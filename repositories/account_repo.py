"""t_account 数据访问。"""

from __future__ import annotations

from datetime import datetime
from typing import Collection, List, Optional, Tuple

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
    """按多个状态查询(启动全起用:已登录 + 需重新登录都试连)。死号不参与启动。"""
    if not statuses:
        return []
    placeholders = ",".join(["%s"] * len(statuses))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM t_account WHERE status IN ({placeholders}) AND is_dead = 0",
                tuple(statuses),
            )
            rows = cur.fetchall()
    return [Account.from_row(r) for r in rows]


def page(
    page_no: int, size: int, keyword: Optional[str], status: Optional[int],
    dead_filter: str = "alive", dead_sort_first: Optional[bool] = None,
) -> Tuple[List[Account], int]:
    """分页 + 搜索(电话/用户名模糊、状态精确)。返回 (记录, 总数)。

    dead_filter:
      - "alive"(默认): 只看正常号 (is_dead=0),用于死号物理隔离场景
      - "all": 全部,死号也看得到 (主列表加了死号过滤下拉后用)
      - "dead": 只看死号 (is_dead=1)
    dead_sort_first:
      - None: 不按 is_dead 排序 → update_time ASC, login_time DESC, id DESC
      - True: 死号靠前 → is_dead DESC + dead_time DESC + update_time ASC + login_time DESC + id DESC
      - False: 死号靠后 → is_dead ASC + update_time ASC + login_time DESC + id DESC

    update_time ASC:把「web 编辑过资料」的号排到最后(改资料会刷新 update_time,
    值越新越靠后),从未编辑过的号 update_time 仍为初始值(≈create_time)排在前面。
    优先级:死号沉底(若启用)> 改过资料沉底 > 登录时间倒序。
    """
    where: list[str] = []
    params: list = []
    if dead_filter == "alive":
        where.append("is_dead = 0")
    elif dead_filter == "dead":
        where.append("is_dead = 1")
    # "all" 不加过滤
    if keyword:
        where.append("(phone LIKE %s OR tg_username LIKE %s OR tg_first_name LIKE %s)")
        like = f"%{keyword}%"
        params += [like, like, like]
    if status is not None:
        where.append("status = %s")
        params.append(status)
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    if dead_sort_first is True:
        order_by = "is_dead DESC, dead_time DESC, update_time ASC, login_time DESC, id DESC"
    elif dead_sort_first is False:
        order_by = "is_dead ASC, update_time ASC, login_time DESC, id DESC"
    else:
        order_by = "update_time ASC, login_time DESC, id DESC"

    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM t_account{clause}", tuple(params))
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                f"SELECT * FROM t_account{clause} ORDER BY {order_by} LIMIT %s OFFSET %s",
                tuple(params + [size, offset]),
            )
            rows = cur.fetchall()
    return [Account.from_row(r) for r in rows], total


def page_dead(
    page_no: int, size: int, keyword: Optional[str],
) -> Tuple[List[Account], int]:
    """死号分页(is_dead=1)。按 dead_time 倒序。"""
    where = ["is_dead = 1"]
    params: list = []
    if keyword:
        where.append("(phone LIKE %s OR tg_username LIKE %s OR tg_first_name LIKE %s OR dead_reason LIKE %s)")
        like = f"%{keyword}%"
        params += [like, like, like, like]
    clause = " WHERE " + " AND ".join(where)

    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM t_account{clause}", tuple(params))
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                f"SELECT * FROM t_account{clause} ORDER BY dead_time DESC, id DESC LIMIT %s OFFSET %s",
                tuple(params + [size, offset]),
            )
            rows = cur.fetchall()
    return [Account.from_row(r) for r in rows], total


def mark_dead(
    phone: str,
    err_code: str,
    err_desc: str,
    expected_statuses: Optional[Collection[int]] = None,
) -> int:
    """打死号标。err_code+desc 拼成 dead_reason 存。

    WHERE is_dead=0 保证只标第一次,后续重复触发的更新返回 0 行(口径口径稳定,
    不覆盖原始失效原因)。同时 status=5,与现有「需重登」语义一致。
    返回受影响行数,>0 说明本次确实把号搬到了死号集合。
    """
    reason = f"{err_code}: {err_desc}"[:255]  # VARCHAR(255) 截断
    now = datetime.now()
    # 不写 update_time:判死不是「编辑资料」。死号有独立的 dead_time 记录判定时间。
    sql = (
        "UPDATE t_account SET is_dead=1, dead_reason=%s, dead_time=%s, "
        "status=5 WHERE phone=%s AND is_dead=0"
    )
    params = [reason, now, phone]
    if expected_statuses is not None:
        statuses = sorted({int(status) for status in expected_statuses})
        if not statuses:
            return 0
        placeholders = ",".join(["%s"] * len(statuses))
        sql += f" AND status IN ({placeholders})"
        params.extend(statuses)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return int(cur.rowcount)


def revive(phone: str) -> int:
    """复活死号:is_dead=0,status=0(未登录,运营走重新登录),清空死号字段。"""
    # 不写 update_time:复活不是「编辑资料」,不影响小号列表排序。
    sql = (
        "UPDATE t_account SET is_dead=0, dead_reason=NULL, dead_time=NULL, "
        "status=0 WHERE phone=%s AND is_dead=1"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone,))
            return int(cur.rowcount)


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
    """协议号导入 upsert(import_type=2)。冲突时更新凭证与 session_path。

    注意:update_time 已被语义专用化为「web 编辑资料时间」(用于小号列表排序,改过
    资料的号沉底)。导入不属于编辑资料,故插入时 update_time 落 create_time(= now),
    冲突更新分支不再触碰 update_time,避免污染排序。
    """
    now = datetime.now()
    sql = (
        "INSERT INTO t_account (phone, import_type, session_path, api_id, api_hash, "
        "device_model, app_version, lang_pack, two_fa, status, create_time, update_time) "
        "VALUES (%s, 2, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s) "
        "ON DUPLICATE KEY UPDATE session_path=VALUES(session_path), api_id=VALUES(api_id), "
        "api_hash=VALUES(api_hash), device_model=VALUES(device_model), "
        "app_version=VALUES(app_version), lang_pack=VALUES(lang_pack), "
        "two_fa=VALUES(two_fa), status=1"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, session_path, api_id, api_hash, device_model,
                              app_version, lang_pack, two_fa, now, now))


def upsert_phone(phone: str, session_path: str) -> None:
    """手机号登录 upsert(import_type=1,用全局凭证,api 字段留空)。

    update_time 语义同 upsert_protocol:插入落 create_time,冲突更新不动它(登录不是编辑资料)。
    """
    now = datetime.now()
    sql = (
        "INSERT INTO t_account (phone, import_type, session_path, status, create_time, update_time) "
        "VALUES (%s, 1, %s, 1, %s, %s) "
        "ON DUPLICATE KEY UPDATE session_path=VALUES(session_path), import_type=1, "
        "status=1"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, session_path, now, now))


def update_status(phone: str, status: int) -> None:
    # 不写 update_time:状态变更不是「web 编辑资料」,不应影响小号列表排序。
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_account SET status=%s WHERE phone=%s",
                (status, phone),
            )


def update_status_if_current(
    phone: str,
    status: int,
    expected_statuses: Collection[int],
) -> int:
    """账号仍存活且处于预期状态时更新状态，返回受影响行数。"""
    statuses = sorted({int(expected) for expected in expected_statuses})
    if not statuses:
        return 0
    placeholders = ",".join(["%s"] * len(statuses))
    sql = (
        f"UPDATE t_account SET status=%s WHERE phone=%s AND is_dead=0 "
        f"AND status IN ({placeholders})"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status, phone, *statuses))
            return int(cur.rowcount)


def mark_logged_in(phone: str, tg_user_id: Optional[int], first_name: Optional[str],
                   last_name: Optional[str], username: Optional[str],
                   reconnect_only: bool = False) -> int:
    """登录成功:回填 TG 信息 + status=3 + login_time。

    不写 update_time:登录回填(含巡检同步 TG 昵称)不是 web 主动编辑资料,
    若刷 update_time 会把巡检同步过的号误判为「改过资料」排到最后,污染排序。
    """
    now = datetime.now()
    sql = (
        "UPDATE t_account SET status=3, tg_user_id=%s, tg_first_name=%s, tg_last_name=%s, "
        "tg_username=%s, login_time=%s"
    )
    if reconnect_only:
        sql += " WHERE phone=%s AND is_dead=0 AND status IN (3,5)"
    else:
        sql += ", is_dead=0, dead_reason=NULL, dead_time=NULL WHERE phone=%s"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (tg_user_id, first_name, last_name, username, now, phone))
            return int(cur.rowcount)


def update_profile(phone: str, first_name: Optional[str], last_name: Optional[str],
                   username: Optional[str], bio: Optional[str],
                   touch_edit_time: bool = True) -> None:
    """回写小号资料字段。

    touch_edit_time:是否刷新 update_time(=「web 编辑资料时间」,小号列表据此把改过
    资料的号排到最后)。
      - True(默认):web 主动「编辑资料」,刷新,使该号沉底。
      - False:巡检 _sync_profile 自动同步 TG 昵称时传,只回写昵称不刷时间,
        否则巡检会把任何在 TG 改过名的号误判为「改过资料」持续污染排序。
    """
    sets: list[str] = []
    params: list = []
    for col, val in (("tg_first_name", first_name), ("tg_last_name", last_name),
                     ("tg_username", username), ("bio", bio)):
        if val is not None:
            sets.append(f"{col}=%s")
            params.append(val)
    if not sets:
        return
    if touch_edit_time:
        sets.append("update_time=%s")
        params.append(datetime.now())
    params.append(phone)
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
    # 不写 update_time:AI 人设不是「编辑资料」,不影响小号列表排序。
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_account SET persona_json=%s WHERE phone=%s",
                (persona_json, phone),
            )


def reset_all_status_on_startup() -> None:
    """服务启动:上次运行态(初始化中1/等待码2/等待2FA 6)统一置离线(4),
    已登录(3)的号保持 3 交给 ClientManager 全起后重连校验回写,
    不动 NEED_RELOGIN(5)。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE t_account SET status=4 WHERE status IN (1,2,6)")


def delete_by_phone(phone: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_account WHERE phone=%s", (phone,))
            return int(cur.rowcount)
