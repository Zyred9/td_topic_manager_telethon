import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from config.constants import AccountStatus
from core import client_manager as client_manager_module
from helpers import dead_account
from repositories import account_repo
from services import account_service, import_service


def _ready_me():
    return SimpleNamespace(id=1, first_name="first", last_name="last", username="user")


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dead_account._account_state_lock = asyncio.Lock()
        dead_account._pending_dead.clear()
        client_manager_module.client_manager._clients.clear()

    async def asyncTearDown(self) -> None:
        dead_account._pending_dead.clear()
        client_manager_module.client_manager._clients.clear()

    async def test_state_changed_during_reconnect_does_not_register_client(self) -> None:
        manager = client_manager_module.ClientManager()
        account = SimpleNamespace(phone="+10000000010", status=int(AccountStatus.LOGGED_IN))
        me = SimpleNamespace(id=1, first_name="first", last_name="last", username="user")
        client = SimpleNamespace(
            connect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=me),
            disconnect=AsyncMock(),
        )

        with (
            patch.object(
                client_manager_module.telethon_factory,
                "build_client_for_account",
                return_value=client,
            ),
            patch.object(client_manager_module, "run_db", new=AsyncMock(return_value=0)) as run_db,
        ):
            await manager.connect_account(account)

        run_db.assert_awaited_once_with(
            account_repo.mark_logged_in,
            account.phone,
            me.id,
            me.first_name,
            me.last_name,
            me.username,
            True,
        )
        self.assertIsNone(manager.get_client(account.phone))
        client.disconnect.assert_awaited_once_with()

    async def test_prepare_startup_resets_pending_statuses_once(self) -> None:
        manager = client_manager_module.ClientManager()

        with patch.object(
            client_manager_module,
            "run_db",
            new=AsyncMock(),
        ) as run_db:
            await manager.prepare_startup()

        run_db.assert_awaited_once_with(account_repo.reset_all_status_on_startup)

    async def test_startup_failure_uses_conditional_reconnect_transition(self) -> None:
        manager = client_manager_module.ClientManager()
        account = SimpleNamespace(phone="+10000000031", status=int(AccountStatus.LOGGED_IN))

        async def startup_db(func, *_args):
            if func is account_repo.find_by_statuses:
                return [account]
            return None

        with (
            patch.object(
                client_manager_module,
                "run_db",
                new=AsyncMock(side_effect=startup_db),
            ) as startup_run_db,
            patch.object(
                manager,
                "connect_account",
                new=AsyncMock(side_effect=RuntimeError("startup failure")),
            ),
            patch.object(
                manager,
                "_mark_reconnect_needed_if_current",
                new=AsyncMock(return_value=False),
            ) as mark_reconnect,
        ):
            await manager.startup()

        mark_reconnect.assert_awaited_once_with(account)
        self.assertEqual(
            [
                call(
                    account_repo.find_by_statuses,
                    [int(AccountStatus.LOGGED_IN), int(AccountStatus.NEED_RELOGIN)],
                )
            ],
            startup_run_db.await_args_list,
        )

    async def test_startup_failure_after_offline_does_not_overwrite_status(self) -> None:
        manager = client_manager_module.ClientManager()
        account = SimpleNamespace(phone="+10000000032", status=int(AccountStatus.LOGGED_IN))

        with patch.object(
            client_manager_module,
            "run_db",
            new=AsyncMock(return_value=0),
        ) as run_db:
            affected = await manager._mark_reconnect_needed_if_current(account)

        self.assertFalse(affected)
        run_db.assert_awaited_once_with(
            account_repo.update_status_if_current,
            account.phone,
            int(AccountStatus.NEED_RELOGIN),
            (int(AccountStatus.LOGGED_IN),),
        )

    async def test_startup_failure_after_new_authorization_does_not_touch_database(self) -> None:
        manager = client_manager_module.ClientManager()
        account = SimpleNamespace(phone="+10000000033", status=int(AccountStatus.LOGGED_IN))
        new_client = SimpleNamespace()
        manager._clients[account.phone] = new_client

        with patch.object(
            client_manager_module,
            "run_db",
            new=AsyncMock(),
        ) as run_db:
            affected = await manager._mark_reconnect_needed_if_current(account)

        self.assertFalse(affected)
        run_db.assert_not_awaited()
        self.assertIs(new_client, manager.get_client(account.phone))

    async def test_unauthorized_session_uses_pending_capable_dead_marker(self) -> None:
        manager = client_manager_module.ClientManager()
        account = SimpleNamespace(phone="+10000000014", status=int(AccountStatus.LOGGED_IN))
        client = SimpleNamespace(
            connect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=False),
            disconnect=AsyncMock(),
        )

        with (
            patch.object(
                client_manager_module.telethon_factory,
                "build_client_for_account",
                return_value=client,
            ),
            patch.object(
                client_manager_module,
                "mark_dead_by_reason",
                new=AsyncMock(),
            ) as mark_dead,
        ):
            await manager.connect_account(account)

        mark_dead.assert_awaited_once_with(
            account.phone,
            "SessionInvalid",
            "连接时 session 已失效(未授权)",
            expected_client=None,
            expected_statuses=(int(AccountStatus.LOGGED_IN),),
        )
        client.disconnect.assert_awaited_once_with()

    async def test_concurrent_ready_client_wins_over_old_session(self) -> None:
        manager = client_manager_module.ClientManager()
        account = SimpleNamespace(phone="+10000000012", status=int(AccountStatus.LOGGED_IN))
        current = SimpleNamespace(is_connected=lambda: True)
        manager._clients[account.phone] = current
        client = SimpleNamespace(
            connect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=SimpleNamespace()),
            disconnect=AsyncMock(),
        )

        with (
            patch.object(
                client_manager_module.telethon_factory,
                "build_client_for_account",
                return_value=client,
            ),
            patch.object(client_manager_module, "run_db", new=AsyncMock()) as run_db,
        ):
            await manager.connect_account(account)

        self.assertIs(current, manager.get_client(account.phone))
        run_db.assert_not_awaited()
        client.disconnect.assert_awaited_once_with()

    async def test_manual_registration_clears_old_pending_dead_mark(self) -> None:
        manager = client_manager_module.ClientManager()
        phone = "+10000000013"
        me = _ready_me()
        dead_account._pending_dead[phone] = (object(), "OldSession", "invalid", None)

        with (
            patch.object(
                manager,
                "_register_ready_client_locked",
                new=AsyncMock(),
            ) as register_locked,
            patch.object(client_manager_module, "run_db", new=AsyncMock(return_value=1)) as run_db,
        ):
            await manager.register_ready_client(phone, SimpleNamespace(), me)

        run_db.assert_awaited_once_with(
            account_repo.mark_logged_in,
            phone,
            me.id,
            me.first_name,
            me.last_name,
            me.username,
            False,
        )
        register_locked.assert_awaited_once()
        self.assertNotIn(phone, dead_account.pending_dead_phones())

    async def test_authorized_database_commit_failure_does_not_register(self) -> None:
        manager = client_manager_module.ClientManager()
        phone = "+10000000020"
        me = _ready_me()
        client = SimpleNamespace()

        with (
            patch.object(client_manager_module, "run_db", new=AsyncMock(return_value=0)),
            patch.object(
                manager,
                "_register_ready_client_locked",
                new=AsyncMock(),
            ) as register_locked,
        ):
            with self.assertRaisesRegex(RuntimeError, "授权状态提交失败"):
                await manager.register_ready_client(phone, client, me)

        register_locked.assert_not_awaited()
        self.assertIsNone(manager.get_client(phone))

    async def test_new_authorized_client_prevents_old_unauthorized_mark(self) -> None:
        manager = client_manager_module.client_manager
        phone = "+10000000017"
        me = _ready_me()
        account = SimpleNamespace(phone=phone, status=int(AccountStatus.LOGGED_IN))
        auth_entered = asyncio.Event()
        release_old_auth = asyncio.Event()

        async def old_is_authorized() -> bool:
            auth_entered.set()
            await release_old_auth.wait()
            return False

        old_client = SimpleNamespace(
            connect=AsyncMock(),
            is_user_authorized=old_is_authorized,
            disconnect=AsyncMock(),
        )

        def on_event(*_args):
            return lambda handler: handler

        new_client = SimpleNamespace(
            on=on_event,
            is_connected=lambda: True,
            disconnect=AsyncMock(),
        )

        with (
            patch.object(
                client_manager_module.telethon_factory,
                "build_client_for_account",
                return_value=old_client,
            ),
            patch.object(client_manager_module, "run_db", new=AsyncMock(return_value=1)),
            patch("infra.db.run_db", new=AsyncMock()) as dead_run_db,
        ):
            reconnect = asyncio.create_task(manager.connect_account(account))
            await auth_entered.wait()
            await manager.register_ready_client(phone, new_client, me)
            release_old_auth.set()
            await reconnect

        dead_run_db.assert_not_awaited()
        self.assertIs(new_client, manager.get_client(phone))
        old_client.disconnect.assert_awaited_once_with()
        new_client.disconnect.assert_not_awaited()

    async def test_pending_retry_cannot_override_authorized_registration(self) -> None:
        manager = client_manager_module.client_manager
        phone = "+10000000018"
        me = _ready_me()
        token = object()
        dead_account._pending_dead[phone] = (token, "OldSession", "invalid", None)
        mark_entered = asyncio.Event()
        release_mark = asyncio.Event()

        async def blocked_mark(*_args) -> int:
            mark_entered.set()
            await release_mark.wait()
            return 1

        def on_event(*_args):
            return lambda handler: handler

        new_client = SimpleNamespace(
            on=on_event,
            is_connected=lambda: True,
            disconnect=AsyncMock(),
        )

        with (
            patch("infra.db.run_db", new=AsyncMock(side_effect=blocked_mark)),
            patch.object(client_manager_module, "run_db", new=AsyncMock(return_value=1)) as revive_db,
        ):
            retry = asyncio.create_task(dead_account.retry_pending_dead_marks())
            await mark_entered.wait()
            registration = asyncio.create_task(manager.register_ready_client(phone, new_client, me))
            await asyncio.sleep(0)
            self.assertFalse(registration.done())
            release_mark.set()
            await retry
            await registration

        revive_db.assert_awaited_once_with(
            account_repo.mark_logged_in,
            phone,
            me.id,
            me.first_name,
            me.last_name,
            me.username,
            False,
        )
        self.assertIs(new_client, manager.get_client(phone))
        self.assertNotIn(phone, dead_account.pending_dead_phones())

    async def test_stale_pending_retry_token_skips_after_registration(self) -> None:
        manager = client_manager_module.client_manager
        phone = "+10000000019"
        me = _ready_me()
        token = object()
        dead_account._pending_dead[phone] = (token, "OldSession", "invalid", None)

        def on_event(*_args):
            return lambda handler: handler

        new_client = SimpleNamespace(
            on=on_event,
            is_connected=lambda: True,
            disconnect=AsyncMock(),
        )

        with (
            patch.object(client_manager_module, "run_db", new=AsyncMock(return_value=1)),
            patch("infra.db.run_db", new=AsyncMock()) as dead_run_db,
            patch.object(manager, "remove", new=AsyncMock()) as remove,
        ):
            await manager.register_ready_client(phone, new_client, me)
            marked = await dead_account._mark_and_remove(
                phone,
                "OldSession",
                "invalid",
                pending_token=token,
            )

        self.assertFalse(marked)
        dead_run_db.assert_not_awaited()
        remove.assert_not_awaited()
        self.assertIs(new_client, manager.get_client(phone))


class OfflineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dead_account._account_state_lock = asyncio.Lock()
        client_manager_module.client_manager._clients.clear()

    async def asyncTearDown(self) -> None:
        client_manager_module.client_manager._clients.clear()

    async def test_offline_persists_status_before_removing_client(self) -> None:
        calls = []

        async def record_db(*args) -> None:
            calls.append(("db", args))

        async def record_remove(phone: str) -> None:
            calls.append(("remove", phone))

        with (
            patch.object(account_service, "run_db", new=AsyncMock(side_effect=record_db)),
            patch.object(
                account_service.client_manager,
                "remove",
                new=AsyncMock(side_effect=record_remove),
            ),
        ):
            await account_service.offline("10000000011")

        self.assertEqual(
            [
                (
                    "db",
                    (
                        account_repo.update_status,
                        "+10000000011",
                        int(AccountStatus.OFFLINE),
                    ),
                ),
                ("remove", "+10000000011"),
            ],
            calls,
        )

    async def test_offline_waits_for_authorized_registration_then_wins(self) -> None:
        manager = client_manager_module.client_manager
        phone = "+10000000021"
        me = _ready_me()
        login_entered = asyncio.Event()
        release_login = asyncio.Event()
        calls = []

        async def commit_login(*_args) -> int:
            calls.append("login_entered")
            login_entered.set()
            await release_login.wait()
            calls.append("login_committed")
            return 1

        async def commit_offline(*args) -> int:
            calls.append(("offline_committed", args))
            return 1

        def on_event(*_args):
            return lambda handler: handler

        client = SimpleNamespace(
            on=on_event,
            is_connected=lambda: True,
            disconnect=AsyncMock(),
        )

        with (
            patch.object(
                client_manager_module,
                "run_db",
                new=AsyncMock(side_effect=commit_login),
            ),
            patch.object(
                account_service,
                "run_db",
                new=AsyncMock(side_effect=commit_offline),
            ),
        ):
            registration = asyncio.create_task(manager.register_ready_client(phone, client, me))
            await login_entered.wait()
            going_offline = asyncio.create_task(account_service.offline(phone))
            await asyncio.sleep(0)
            self.assertEqual(["login_entered"], calls)
            release_login.set()
            await registration
            await going_offline

        self.assertEqual(
            [
                "login_entered",
                "login_committed",
                (
                    "offline_committed",
                    (account_repo.update_status, phone, int(AccountStatus.OFFLINE)),
                ),
            ],
            calls,
        )
        self.assertIsNone(manager.get_client(phone))
        client.disconnect.assert_awaited_once_with()


class DeadEntrypointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dead_account._account_state_lock = asyncio.Lock()
        client_manager_module.client_manager._clients.clear()

    async def asyncTearDown(self) -> None:
        client_manager_module.client_manager._clients.clear()

    async def test_login_terminal_error_uses_common_dead_marker(self) -> None:
        phone = "+10000000015"
        client = SimpleNamespace(disconnect=AsyncMock())
        account_service._pending_login[phone] = (client, "hash", None)
        exc = RuntimeError("terminal")

        with patch.object(
            account_service,
            "mark_dead_and_remove",
            new=AsyncMock(),
        ) as mark_dead:
            error = await account_service._login_mark_dead(phone, client, exc)

        mark_dead.assert_awaited_once_with(
            phone,
            exc,
            expected_client=None,
            expected_statuses=(
                int(AccountStatus.INITIALIZING),
                int(AccountStatus.WAIT_CODE),
                int(AccountStatus.WAIT_2FA),
            ),
        )
        client.disconnect.assert_awaited_once_with()
        self.assertNotIn(phone, account_service._pending_login)
        self.assertIsInstance(error, account_service.AccountError)

    async def test_import_unauthorized_uses_common_dead_marker(self) -> None:
        phone = "+10000000016"
        client = SimpleNamespace(
            connect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=False),
            disconnect=AsyncMock(),
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            sessions_dir = root / "sessions"
            source_dir.mkdir()
            sessions_dir.mkdir()
            (source_dir / "account.session").write_bytes(b"session")

            with (
                patch.object(import_service, "run_db", new=AsyncMock(return_value=None)),
                patch.object(
                    import_service.telethon_factory,
                    "build_client_for_phone",
                    return_value=client,
                ),
                patch.object(
                    import_service,
                    "mark_dead_by_reason",
                    new=AsyncMock(),
                ) as mark_dead,
                patch.object(import_service.batch_store, "set_item"),
            ):
                await import_service._import_one(phone, source_dir, sessions_dir, "batch")

        mark_dead.assert_awaited_once_with(
            phone,
            "SessionInvalid",
            "导入校验时 session 已失效(未授权)",
            expected_client=None,
            expected_statuses=import_service._IMPORT_PENDING_STATUSES,
        )
        client.disconnect.assert_awaited_once_with()

    async def test_import_failure_after_new_authorization_does_not_downgrade(self) -> None:
        phone = "+10000000029"
        connect_entered = asyncio.Event()
        release_connect = asyncio.Event()

        async def old_connect() -> None:
            connect_entered.set()
            await release_connect.wait()
            raise RuntimeError("old import failure")

        def on_event(*_args):
            return lambda handler: handler

        old_client = SimpleNamespace(connect=old_connect, disconnect=AsyncMock())
        new_client = SimpleNamespace(
            on=on_event,
            is_connected=lambda: True,
            disconnect=AsyncMock(),
        )
        me = _ready_me()

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            sessions_dir = root / "sessions"
            source_dir.mkdir()
            sessions_dir.mkdir()
            (source_dir / "account.session").write_bytes(b"session")

            with (
                patch.object(import_service, "run_db", new=AsyncMock(return_value=None)) as import_db,
                patch.object(
                    import_service.telethon_factory,
                    "build_client_for_phone",
                    return_value=old_client,
                ),
                patch.object(import_service.batch_store, "set_item"),
                patch.object(client_manager_module, "run_db", new=AsyncMock(return_value=1)),
            ):
                importing = asyncio.create_task(
                    import_service._import_one(phone, source_dir, sessions_dir, "batch")
                )
                await connect_entered.wait()
                await client_manager_module.client_manager.register_ready_client(phone, new_client, me)
                release_connect.set()
                await importing

        self.assertNotIn(
            call(
                account_repo.update_status_if_current,
                phone,
                int(AccountStatus.NEED_RELOGIN),
                import_service._IMPORT_PENDING_STATUSES,
            ),
            import_db.await_args_list,
        )
        self.assertIs(new_client, client_manager_module.client_manager.get_client(phone))

    async def test_import_failure_after_offline_keeps_offline_status(self) -> None:
        phone = "+10000000030"
        connect_entered = asyncio.Event()
        release_connect = asyncio.Event()

        async def old_connect() -> None:
            connect_entered.set()
            await release_connect.wait()
            raise RuntimeError("old import failure")

        async def import_db_result(func, *_args):
            if func is account_repo.update_status_if_current:
                return 0
            return None

        old_client = SimpleNamespace(connect=old_connect, disconnect=AsyncMock())

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            sessions_dir = root / "sessions"
            source_dir.mkdir()
            sessions_dir.mkdir()
            (source_dir / "account.session").write_bytes(b"session")

            with (
                patch.object(
                    import_service,
                    "run_db",
                    new=AsyncMock(side_effect=import_db_result),
                ) as import_db,
                patch.object(
                    import_service.telethon_factory,
                    "build_client_for_phone",
                    return_value=old_client,
                ),
                patch.object(import_service.batch_store, "set_item"),
                patch.object(account_service, "run_db", new=AsyncMock(return_value=1)),
            ):
                importing = asyncio.create_task(
                    import_service._import_one(phone, source_dir, sessions_dir, "batch")
                )
                await connect_entered.wait()
                await account_service.offline(phone)
                release_connect.set()
                await importing

        self.assertIn(
            call(
                account_repo.update_status_if_current,
                phone,
                int(AccountStatus.NEED_RELOGIN),
                import_service._IMPORT_PENDING_STATUSES,
            ),
            import_db.await_args_list,
        )
        self.assertIsNone(client_manager_module.client_manager.get_client(phone))


if __name__ == "__main__":
    unittest.main()
