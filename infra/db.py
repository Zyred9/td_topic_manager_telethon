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
