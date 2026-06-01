"""清理 sessions/ 目录中失败的 session 文件。

逻辑：查数据库 status=3（已登录）的号，sessions/ 目录里不在这个列表的全部删除。
在服务器上执行：
    source /etc/td-backend.env && /root/miniconda3/envs/td_topic/bin/python cleanup_sessions.py
"""

import os
from pathlib import Path

import pymysql
import pymysql.cursors

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"


def get_db_connection():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("TDTM_DB_USER") or os.getenv("DB_USER", "root"),
        password=os.getenv("TDTM_DB_PASSWORD") or os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "td_topic_manager"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_logged_in_phones() -> set:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM t_account WHERE status = 3")
            rows = cur.fetchall()
        return {row["phone"] for row in rows}
    finally:
        conn.close()


def main():
    logged_in = get_logged_in_phones()
    print(f"数据库已登录号数量: {len(logged_in)}")

    session_files = list(SESSIONS_DIR.glob("*.session"))
    print(f"sessions/ 目录文件数量: {len(session_files)}")

    deleted = []
    kept = []
    for f in session_files:
        phone = f.stem
        if phone in logged_in:
            kept.append(phone)
        else:
            f.unlink()
            deleted.append(phone)

    print(f"\n保留（已登录）: {len(kept)} 个")
    for p in sorted(kept):
        print(f"  ✓ {p}")

    print(f"\n删除（失败/无效）: {len(deleted)} 个")
    for p in sorted(deleted):
        print(f"  ✗ {p}")


if __name__ == "__main__":
    main()
