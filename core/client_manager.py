"""TelegramClient 客户端池(单进程多 client,共享主 event loop)。

职责:
- 维护 phone -> TelegramClient
- 启动全起:扫已登录小号逐个 connect + 授权校验,注册 NewMessage 事件
- 失效处理:连接断开/未授权 → status=5 需重新登录,移出池
- 给 message_sender / 各 service 提供就绪 client
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from telethon import TelegramClient, errors, events, functions, types

from config.constants import AccountStatus
from core import update_router
from helpers.dead_account import (
    account_state_lock,
    clear_pending_dead,
    is_dead_error,
    mark_dead_and_remove,
    mark_dead_by_reason,
    pending_dead_phones,
)
from infra import telethon_factory
from infra.db import run_db
from repositories import account_repo

logger = logging.getLogger(__name__)


class ClientManager:
    def __init__(self) -> None:
        self._clients: Dict[str, TelegramClient] = {}
        self._lock = asyncio.Lock()

    # ---------- 查询 ----------
    def get_client(self, phone: str) -> Optional[TelegramClient]:
        return self._clients.get(phone)

    def get_ready_client(self, phone: str) -> Optional[TelegramClient]:
        """返回已连接的 client,否则 None。"""
        client = self._clients.get(phone)
        if client is not None and client.is_connected():
            return client
        return None

    def is_logged_in(self, phone: str) -> bool:
        return phone in self._clients

    def all_phones(self) -> list[str]:
        return list(self._clients.keys())

    # ---------- 全起 ----------
    # 单个号启动连接超时(秒),防某个号卡死拖垮整个启动
    CONNECT_TIMEOUT = 15.0

    async def prepare_startup(self) -> None:
        """进入并发启动前重置上次运行遗留的登录中状态。"""
        await run_db(account_repo.reset_all_status_on_startup)

    async def startup(
        self,
        only_phone: Optional[str] = None,
        only_username: Optional[str] = None,
    ) -> None:
        """后台连接"已登录(3)"和"需重新登录(5)"的小号。

        协议号 session 是已登录态凭证,connect 即生效不需验证码,所以状态 5 的号
        若只是上次临时连接失败被误标,本次能连上就自动恢复成状态 3。
        连接结果分三类:
          - session 已失效/被作废(未授权 / AuthKeyDuplicated):终态无用 → 判死进死号列表;
          - 超时 / 其它未知异常:可能临时故障 → 维持 NEED_RELOGIN(5)留主列表,下次再试;
          - 连上且已授权:恢复为已登录(3)。
        """
        accounts = await run_db(
            account_repo.find_by_statuses,
            [int(AccountStatus.LOGGED_IN), int(AccountStatus.NEED_RELOGIN)],
        )
        if only_phone or only_username:
            phone = (only_phone or "").lstrip("+")
            username = (only_username or "").casefold()
            phone_matches = [
                account for account in accounts
                if bool(phone) and str(account.phone).lstrip("+") == phone
            ]
            username_matches = [
                account for account in accounts
                if bool(username)
                and (getattr(account, "tg_username", "") or "").casefold() == username
            ]
            accounts = phone_matches or username_matches
            if len(accounts) > 1:
                logger.error(
                    "启动定点加载:目标匹配到 %d 条账号,为避免误加载已全部跳过",
                    len(accounts),
                )
                accounts = []
            logger.warning(
                "启动定点加载:仅加载 phone=%s username=%s,匹配账号数=%d",
                only_phone,
                only_username,
                len(accounts),
            )
        logger.info("启动全起:发现 %d 个小号待试连(含需重新登录的)", len(accounts))
        for acc in accounts:
            try:
                await asyncio.wait_for(self.connect_account(acc), timeout=self.CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("全起小号超时(%.0fs),跳过 phone=%s", self.CONNECT_TIMEOUT, acc.phone)
                await self._mark_reconnect_needed_if_current(acc)
            except errors.AuthKeyDuplicatedError as exc:
                # session 在多个 IP 同时使用被 TG 作废,是明确终态失效、无法恢复,
                # 该号对运营已无用 → 判死进死号列表(不是临时故障,不刷堆栈)。
                logger.warning(
                    "小号 %s 的 session 已被 Telegram 作废(同一 session 在多个 IP 使用),判定死号",
                    acc.phone,
                )
                await mark_dead_and_remove(
                    acc.phone,
                    exc,
                    expected_client=None,
                    expected_statuses=(int(acc.status),),
                )
            except Exception as exc:
                # connect()/get_me() 直接抛出的终态死号异常(UnauthorizedError 系等)会落到
                # 这里:该号已不可恢复,判死进死号列表,不再降级为 NEED_RELOGIN 赖在主列表。
                # (此时号尚未进池,直接 mark_dead 即可,无需出池。)
                if is_dead_error(exc):
                    logger.warning("全起小号死号异常 phone=%s reason=%s", acc.phone, type(exc).__name__)
                    await mark_dead_and_remove(
                        acc.phone,
                        exc,
                        expected_client=None,
                        expected_statuses=(int(acc.status),),
                    )
                else:
                    logger.exception("全起小号失败 phone=%s", acc.phone)
                    await self._mark_reconnect_needed_if_current(acc)

    async def _mark_reconnect_needed_if_current(self, account) -> bool:
        """池仍为空且账号状态未离开启动快照时，才提交重连失败。"""
        async with account_state_lock():
            if self.get_client(account.phone) is not None:
                return False
            affected = await run_db(
                account_repo.update_status_if_current,
                account.phone,
                int(AccountStatus.NEED_RELOGIN),
                (int(account.status),),
            )
            return bool(affected)

    async def connect_account(self, account) -> None:
        """使用账号现有 session 连接并注册到 client 池。"""
        client = telethon_factory.build_client_for_account(account)
        registered = False
        try:
            await client.connect()
            observation_target = (
                str(account.phone).lstrip("+") == "8801736120330"
                or (getattr(account, "tg_username", "") or "").casefold() == "linxi9687"
            )
            authorized = await client.is_user_authorized()
            if observation_target:
                logger.warning(
                    "[封号特征观察] phone=%s authorized=%s",
                    account.phone,
                    authorized,
                )
            if not authorized:
                # 启动全起时 session 已失效:authkey 不被服务端承认,该号对运营已无用
                # (协议号无法靠验证码重登,手机号也需重新走登录流程)。直接判死进死号
                # 列表,由运营复活或彻底删除,不再赖在主列表里反复点「重新登录」也救不活。
                logger.warning("小号 %s 会话已失效,判定死号", account.phone)
                await mark_dead_by_reason(
                    account.phone, "SessionInvalid", "连接时 session 已失效(未授权)",
                    expected_client=None,
                    expected_statuses=(int(account.status),),
                )
                return
            me = await client.get_me()
            # ponytail: 临时定点观察封号账号，确认特征后删除这段诊断。
            observation_target = observation_target or (
                getattr(me, "username", "") or ""
            ).casefold() == "linxi9687"
            async with account_state_lock():
                if account.phone in pending_dead_phones():
                    logger.info("小号 %s 存在待落库死号标记,跳过旧 session 注册", account.phone)
                    return
                async with self._lock:
                    existing = self._clients.get(account.phone)
                    if existing is not None and existing.is_connected():
                        logger.info("小号 %s 已由并发登录接管,跳过旧 session 注册", account.phone)
                        return
                    affected = await run_db(
                        account_repo.mark_logged_in,
                        account.phone,
                        getattr(me, "id", None),
                        getattr(me, "first_name", None),
                        getattr(me, "last_name", None),
                        getattr(me, "username", None),
                        True,
                    )
                    if not affected:
                        logger.info("小号 %s 重连提交已失效,不注册 client", account.phone)
                        return
                    await self._register_ready_client_locked(account.phone, client)
                    registered = True  # 已进池,交给池管理,后续不在此 disconnect
            logger.info("小号 %s 重连成功 tg_user_id=%s", account.phone, getattr(me, "id", None))
            if observation_target:
                logger.warning(
                    "[封号特征观察] 核心字段 phone=%s me_type=%s id=%s "
                    "tg_phone=%s username=%s deleted=%s restricted=%s "
                    "restriction_reason=%s",
                    account.phone,
                    type(me).__name__,
                    getattr(me, "id", None),
                    getattr(me, "phone", None),
                    getattr(me, "username", None),
                    getattr(me, "deleted", None),
                    getattr(me, "restricted", None),
                    getattr(me, "restriction_reason", None),
                )
                logger.warning(
                    "[封号特征观察] phone=%s get_me=%s",
                    account.phone,
                    me.stringify() if hasattr(me, "stringify") else repr(me),
                )
                try:
                    full_user = await asyncio.wait_for(
                        client(functions.users.GetFullUserRequest(types.InputUserSelf())),
                        timeout=5.0,
                    )
                    logger.warning(
                        "[封号特征观察] phone=%s full_user_type=%s full_user=%s",
                        account.phone,
                        type(full_user).__name__,
                        full_user.stringify()
                        if hasattr(full_user, "stringify")
                        else repr(full_user),
                    )
                except Exception as exc:
                    logger.warning(
                        "[封号特征观察] phone=%s 拉取 full_user 失败 reason=%s",
                        account.phone,
                        type(exc).__name__,
                        exc_info=exc,
                    )
        except Exception as exc:
            if is_dead_error(exc):
                await mark_dead_and_remove(
                    account.phone,
                    exc,
                    expected_client=None,
                    expected_statuses=(int(account.status),),
                )
                return
            raise
        finally:
            # 未进池的 client(失效/异常)一律断开,防半开连接泄漏
            if not registered:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    # ---------- 注册 / 注销 ----------
    def _register_events(self, phone: str, client: TelegramClient) -> None:
        @client.on(events.NewMessage)
        async def _on_new_message(event):  # noqa: ANN001
            await update_router.dispatch(phone, event)

    async def register_ready_client(self, phone: str, client: TelegramClient, me) -> None:
        """登录服务(协议号/手机号)完成授权后,把 client 交给池统一持有。"""
        async with account_state_lock():
            affected = await run_db(
                account_repo.mark_logged_in,
                phone,
                getattr(me, "id", None),
                getattr(me, "first_name", None),
                getattr(me, "last_name", None),
                getattr(me, "username", None),
                False,
            )
            if not affected:
                raise RuntimeError(f"账号 {phone} 授权状态提交失败,未注册 client")
            async with self._lock:
                await self._register_ready_client_locked(phone, client)
                clear_pending_dead(phone)

    async def _register_ready_client_locked(self, phone: str, client: TelegramClient) -> None:
        """调用方持有 _lock 时替换 client。"""
        old = self._clients.get(phone)
        if old is not None and old is not client:
            try:
                await old.disconnect()
            except Exception:
                pass
        self._register_events(phone, client)
        self._clients[phone] = client

    async def remove(self, phone: str) -> None:
        """移出池并断开(删除小号/失效时)。"""
        async with self._lock:
            client = self._clients.pop(phone, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("断开 client 失败 phone=%s", phone)

    # 单个 client disconnect 超时(秒):防 Telethon 半开连接阻塞拖住整个服务关闭
    _DISCONNECT_TIMEOUT = 5.0

    async def shutdown(self) -> None:
        """服务关闭:断开所有 client。逐个加超时,半开连接超时即放弃(进程退出由 OS 回收)。"""
        phones = list(self._clients.keys())
        for phone in phones:
            try:
                await asyncio.wait_for(self.remove(phone), timeout=self._DISCONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("断开 client 超时(%.0fs),跳过 phone=%s", self._DISCONNECT_TIMEOUT, phone)
            except Exception as exc:
                logger.warning("断开 client 异常 phone=%s", phone, exc_info=exc)
        logger.info("已断开全部 %d 个小号 client", len(phones))


# 全局单例
client_manager = ClientManager()
