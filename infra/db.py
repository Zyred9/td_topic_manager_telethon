"""MySQL 连接与初始化。

风格沿用 baihui:pymysql + 原生 SQL + ``@contextmanager get_connection()``,
自动 commit/rollback。pymysql 是同步库,在 async 场景下 repository 调用统一经
``run_db`` 包进线程,避免阻塞 event loop。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import pymysql
from pymysql.connections import Connection

from config.settings import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


@contextmanager
def get_connection() -> Iterator[Connection]:
    """产出一个自动 commit/rollback 的 MySQL 连接。"""
    cfg = get_settings().db
    connection = pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        autocommit=False,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在线程池执行同步 DB 操作,避免阻塞 asyncio event loop。"""
    return await asyncio.to_thread(func, *args, **kwargs)


def _ensure_database_exists() -> None:
    """库不存在时先建库(连不带 database)。"""
    cfg = get_settings().db
    connection = pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        autocommit=True,
        charset="utf8mb4",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg.database}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
            )
    finally:
        connection.close()


def _split_statements(sql_text: str) -> list[str]:
    """拆分建表语句:先逐行剥离 ``--`` 注释(避免注释内分号干扰),再按分号拆。

    注意先去注释再拆分,否则注释行内的分号会把注释切断、残片被误当 SQL。
    schema.sql 不含存储过程,也不在字符串字面量里放分号,简单分号拆分即可。
    """
    # 1. 逐行剥离 -- 注释(整行注释或行尾注释)
    cleaned_lines: list[str] = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        if line.strip():
            cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)

    # 2. 按分号拆,但跳过单引号字符串字面量内的分号(COMMENT '...;...' 不被切断)
    statements: list[str] = []
    buf: list[str] = []
    in_str = False
    i = 0
    while i < len(cleaned):
        ch = cleaned[i]
        if ch == "'":
            # 处理 SQL 转义的连续两个单引号 ''
            if in_str and i + 1 < len(cleaned) and cleaned[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_str = not in_str
            buf.append(ch)
        elif ch == ";" and not in_str:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def init_schema() -> None:
    """建库 + 执行 schema.sql 建表(幂等)。"""
    _ensure_database_exists()
    schema_path: Path = BASE_DIR / "sql" / "schema.sql"
    sql_text = schema_path.read_text(encoding="utf-8")
    statements = _split_statements(sql_text)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            for stmt in statements:
                cursor.execute(stmt)
    logger.info("数据库表初始化完成,共执行 %d 条建表语句", len(statements))
    _run_migrations()


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cursor.fetchone()
    return bool(row and row["cnt"])


def _index_exists(cursor, table: str, index: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
        (table, index),
    )
    row = cursor.fetchone()
    return bool(row and row["cnt"])


def _run_migrations() -> None:
    """对已存在的老库做幂等补列(新库 schema.sql 已含,跳过)。"""
    migrations = [
        ("t_account_watch", "chat_title",
         "ALTER TABLE t_account_watch ADD COLUMN chat_title VARCHAR(255) "
         "COMMENT '群名(加群时回填,展示用)' AFTER chat_id"),
        # 死号标记三列 + 索引(2026-05-28:加发送日志 + 死号列表)
        ("t_account", "is_dead",
         "ALTER TABLE t_account ADD COLUMN is_dead TINYINT NOT NULL DEFAULT 0 "
         "COMMENT '0正常 1已失效(死号,默认从主列表过滤)' AFTER persona_json"),
        ("t_account", "dead_reason",
         "ALTER TABLE t_account ADD COLUMN dead_reason VARCHAR(255) "
         "COMMENT '失效原因(Telethon 异常类名+描述)' AFTER is_dead"),
        ("t_account", "dead_time",
         "ALTER TABLE t_account ADD COLUMN dead_time DATETIME "
         "COMMENT '判定失效时间' AFTER dead_reason"),
    ]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for table, column, ddl in migrations:
                if not _column_exists(cursor, table, column):
                    cursor.execute(ddl)
                    logger.info("迁移:%s 新增列 %s", table, column)
            # 索引补建(列已存在但旧库索引未建时)
            if (_column_exists(cursor, "t_account", "is_dead")
                    and not _index_exists(cursor, "t_account", "idx_is_dead")):
                cursor.execute("ALTER TABLE t_account ADD INDEX idx_is_dead (is_dead)")
                logger.info("迁移:t_account 新增索引 idx_is_dead")
