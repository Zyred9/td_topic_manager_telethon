"""t_persona_preset 数据访问(自定义人设模板)。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from infra.db import get_connection


def list_all() -> List[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_persona_preset ORDER BY id ASC")
            return cur.fetchall()


def insert(preset_name: str, preset: dict) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO t_persona_preset (preset_name, preset_json, create_time) VALUES (%s, %s, %s)",
                (preset_name, json.dumps(preset, ensure_ascii=False), datetime.now()),
            )
            return int(cur.lastrowid)


def update(preset_id: int, preset_name: str, preset: dict) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE t_persona_preset SET preset_name=%s, preset_json=%s WHERE id=%s",
                (preset_name, json.dumps(preset, ensure_ascii=False), preset_id),
            )
            return int(cur.rowcount)


def delete(preset_id: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_persona_preset WHERE id=%s", (preset_id,))
            return int(cur.rowcount)


def find_one(preset_id: int) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM t_persona_preset WHERE id=%s", (preset_id,))
            return cur.fetchone()
