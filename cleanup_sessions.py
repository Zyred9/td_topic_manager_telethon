"""清理 sessions/ 目录中失败的 session 文件。

逻辑：查数据库 status=3（已登录）的号，sessions/ 目录里不在这个列表的全部删除。
在服务器上执行：
    /root/miniconda3/envs/td_topic/bin/python cleanup_sessions.py
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")  # 显式加载,避免工作目录不同导致 .env 读不到

from config.settings import get_settings
from infra.db import get_connection

settings = get_settings()
sessions_dir = settings.sessions_dir


def get_logged_in_phones() -> set:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM t_account WHERE status = 3")
            rows = cur.fetchall()
    return {row["phone"] for row in rows}


def main():
    logged_in = get_logged_in_phones()
    print(f"数据库已登录号数量: {len(logged_in)}")

    session_files = list(sessions_dir.glob("*.session"))
    print(f"sessions/ 目录文件数量: {len(session_files)}")

    deleted = []
    kept = []
    for f in session_files:
        # 文件名即手机号，如 +8801300308356.session
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
