"""MySQL 连接与初始化。

风格沿用 baihui:pymysql + 原生 SQL + ``@contextmanager get_connection()``,
自动 commit/rollback。pymysql 是同步库,在 async 场景下 repository 调用统一经
``run_db`` 包进线程,避免阻塞 event loop。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import pymysql
from pymysql.connections import Connection

from config.settings import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _ConnectionPool:
    """轻量 MySQL 连接池(纯标准库 + pymysql,零额外依赖)。

    解决 FD 耗尽:此前每次 ``get_connection()`` 都新建一条 TCP 连接用完即弃,
    高频(web + 巡检 + 自驱)下瞬时建连密集、徒增 socket FD 占用。改为复用固定上限
    的连接,把 DB 侧并发连接钳在 ``pool_max`` 以内,空闲连接复用减少建连开销。

    线程安全:repository 全部经 ``run_db`` 跑在 ``asyncio.to_thread`` 线程池里,
    故池用 ``queue.Queue`` 做跨线程的连接借还,不依赖 event loop。
    借出超过 ``pool_max`` 时阻塞等待空闲连接(``Queue.get`` 自带等待)。
    """

    def __init__(self, max_size: int, min_size: int) -> None:
        self._max_size = max(1, max_size)
        self._idle: queue.Queue[Connection] = queue.Queue(maxsize=self._max_size)
        self._lock = threading.Lock()
        # 已创建(在用 + 空闲)的连接总数,用于决定是否还能新建
        self._created = 0
        # 预热:启动即建 min_size 条,避免首批请求集中建连
        for _ in range(max(0, min(min_size, self._max_size))):
            self._idle.put_nowait(self._new_connection())
            self._created += 1

    def _new_connection(self) -> Connection:
        cfg = get_settings().db
        return pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            autocommit=False,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )

    def borrow(self) -> Connection:
        """借一条可用连接:① 优先复用空闲;② 无空闲且未达上限则新建;③ 已达上限阻塞等归还。"""
        # 1) 优先拿空闲连接(非阻塞),复用为主、新建为辅
        try:
            conn = self._idle.get_nowait()
            return self._ensure_alive(conn)
        except queue.Empty:
            pass
        # 2) 无空闲:未达上限可新建一条
        with self._lock:
            can_create = self._created < self._max_size
            if can_create:
                self._created += 1
        if can_create:
            try:
                return self._new_connection()
            except Exception:
                # 建连失败要回退计数,否则池子会永久少一个名额
                with self._lock:
                    self._created -= 1
                raise
        # 3) 已达上限:阻塞等空闲连接归还,把并发连接钳在 max_size;ping 校验存活
        conn = self._idle.get()
        return self._ensure_alive(conn)

    def _ensure_alive(self, conn: Connection) -> Connection:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return self._new_connection()

    def give_back(self, conn: Connection, broken: bool = False) -> None:
        """归还连接。broken=True(执行中抛错且无法复用)则丢弃并补名额。"""
        if broken:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1
            return
        try:
            self._idle.put_nowait(conn)
        except queue.Full:
            # 理论上不会满(借还配对),兜底直接关
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1


_pool: _ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> _ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                cfg = get_settings().db
                _pool = _ConnectionPool(cfg.pool_max, cfg.pool_min)
                logger.info("MySQL 连接池已初始化 max=%d min=%d", cfg.pool_max, cfg.pool_min)
    return _pool


@contextmanager
def get_connection() -> Iterator[Connection]:
    """从连接池借一条自动 commit/rollback 的 MySQL 连接,用完归还(不真断)。

    对调用方语义与旧版完全一致(``with get_connection() as conn:``),仅底层由
    「每次新建」改为「池内复用」,以止住 socket FD 耗尽并降低建连开销。
    """
    pool = _get_pool()
    connection = pool.borrow()
    broken = False
    try:
        yield connection
        connection.commit()
    except Exception:
        # 回滚失败说明连接本身坏了(网络断/服务端踢),标记 broken 由池丢弃重建
        try:
            connection.rollback()
        except Exception:
            broken = True
        raise
    finally:
        pool.give_back(connection, broken=broken)


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
