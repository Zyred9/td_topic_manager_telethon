"""t_send_log 数据访问。

发送日志记录所有发送结果(成功/失败),前端按号 + 时间倒序查。
失败时存原始异常类名 + repr(exc),保留 Telethon 给的全部细节。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from infra.db import get_connection


# 内容预览只截前 200 字,与 schema VARCHAR(200) 对齐
_PREVIEW_MAX = 200


def insert(
    phone: str,
    chat_id: int,
    source: str,
    ok: bool,
    err_code: Optional[str] = None,
    err_message: Optional[str] = None,
    content_preview: Optional[str] = None,
) -> None:
    """记一条发送日志。失败时 err_code/err_message 必传,成功时可全 None。"""
    now = datetime.now()
    preview = (content_preview or "")[:_PREVIEW_MAX]
    sql = (
        "INSERT INTO t_send_log (phone, chat_id, source, ok, err_code, err_message, "
        "content_preview, send_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                phone, int(chat_id), source, 1 if ok else 0,
                err_code, err_message, preview, now,
            ))


def count_results_since(phone: str, hours: int) -> Tuple[int, int]:
    """统计某号近 hours 小时的发送结果,返回 (失败数, 成功数)。

    供巡检判「发送受限」死号:号能登录但近期持续发不出(失败多且零成功)。
    NOT_READY/QUOTA_FULL 这类本地拦截(还没真发出去)的失败不计入,避免误判 —
    只统计真正走到 client.send_message 才失败的网络/权限类失败。
    """
    since = datetime.now() - timedelta(hours=hours)
    sql = (
        "SELECT "
        "SUM(CASE WHEN ok=0 AND err_code NOT IN ('NOT_READY','QUOTA_FULL') THEN 1 ELSE 0 END) AS fail, "
        "SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_cnt "
        "FROM t_send_log WHERE phone=%s AND send_time >= %s"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (phone, since))
            row = cur.fetchone() or {}
    return int(row.get("fail") or 0), int(row.get("ok_cnt") or 0)


def phones_with_consecutive_send_fails(min_streak: int) -> List[str]:
    """找出「最近连续发送失败 ≥ min_streak 次」的号,供启动扫描判死。

    连续口径:号的最后一条成功之后(没成功过则从头)的失败条数 ≥ min_streak。
    等价于按时间倒序最近 min_streak 条全是失败。只算真走到 send_message 的失败,
    排除 NOT_READY/QUOTA_FULL 这类本地拦截(还没真发出去,不代表号发不出)。

    用 last_ok(该号最后一次成功时间)当分界:统计 send_time > last_ok 的失败数。
    没成功过的号 last_ok 为 NULL,用 '1970-01-01' 兜底使其全部失败计入。
    """
    sql = (
        "SELECT f.phone "
        "FROM ("
        "  SELECT phone, "
        "         COALESCE(MAX(CASE WHEN ok=1 THEN send_time END), '1970-01-01') AS last_ok "
        "  FROM t_send_log GROUP BY phone"
        ") f "
        "JOIN t_send_log l ON l.phone = f.phone "
        "  AND l.ok = 0 "
        "  AND l.err_code NOT IN ('NOT_READY','QUOTA_FULL') "
        "  AND l.send_time > f.last_ok "
        "GROUP BY f.phone "
        "HAVING COUNT(*) >= %s"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (int(min_streak),))
            rows = cur.fetchall()
    return [r["phone"] for r in rows]


def page_by_phone(
    phone: str, page_no: int, size: int,
    only_failed: bool = False, hours: Optional[int] = None,
) -> Tuple[List[dict], int]:
    """按号分页,可筛只看失败 / 时间窗口(最近 N 小时)。按时间倒序。"""
    where = ["phone = %s"]
    params: list = [phone]
    if only_failed:
        where.append("ok = 0")
    if hours is not None and hours > 0:
        where.append("send_time >= %s")
        params.append(datetime.now() - timedelta(hours=hours))
    clause = " WHERE " + " AND ".join(where)

    if page_no < 1:
        page_no = 1
    offset = (page_no - 1) * size

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM t_send_log{clause}", tuple(params))
            total = int(cur.fetchone()["cnt"])
            cur.execute(
                f"SELECT * FROM t_send_log{clause} ORDER BY send_time DESC, id DESC LIMIT %s OFFSET %s",
                tuple(params + [size, offset]),
            )
            rows = cur.fetchall()
    return list(rows), total
