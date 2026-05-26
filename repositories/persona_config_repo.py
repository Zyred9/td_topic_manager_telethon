"""人设配置标签库(性格/兴趣/emoji)数据访问。

三张表结构同构:id + 值列 + create_time + 值列唯一。用 (table, value_col) 参数化复用。
"""

from __future__ import annotations

from datetime import datetime
from typing import List

from infra.db import get_connection

# 表名 → 值列名
_TABLES = {
    "tone": ("t_persona_tone", "label"),
    "interest": ("t_persona_interest", "label"),
    "emoji": ("t_persona_emoji", "emoji"),
}


def _resolve(kind: str) -> tuple[str, str]:
    if kind not in _TABLES:
        raise ValueError(f"未知的人设配置类型: {kind}")
    return _TABLES[kind]


def list_all(kind: str) -> List[dict]:
    table, col = _resolve(kind)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, {col} AS value FROM {table} ORDER BY id ASC")
            return cur.fetchall()


def list_values(kind: str) -> List[str]:
    """只返回值列表(供 AI 引擎/人设页用)。"""
    return [r["value"] for r in list_all(kind)]


def insert(kind: str, value: str) -> int:
    table, col = _resolve(kind)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table} ({col}, create_time) VALUES (%s, %s)",
                (value, datetime.now()),
            )
            return int(cur.lastrowid)


def update(kind: str, item_id: int, value: str) -> int:
    table, col = _resolve(kind)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET {col}=%s WHERE id=%s", (value, item_id))
            return int(cur.rowcount)


def delete(kind: str, item_id: int) -> int:
    table, col = _resolve(kind)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE id=%s", (item_id,))
            return int(cur.rowcount)


def count(kind: str) -> int:
    table, col = _resolve(kind)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
            return int(cur.fetchone()["cnt"])


def init_defaults(kind: str, values: List[str]) -> None:
    """表空时灌入默认值(首次启动初始化)。"""
    if count(kind) > 0:
        return
    table, col = _resolve(kind)
    now = datetime.now()
    with get_connection() as conn:
        with conn.cursor() as cur:
            for v in values:
                cur.execute(
                    f"INSERT IGNORE INTO {table} ({col}, create_time) VALUES (%s, %s)",
                    (v, now),
                )
