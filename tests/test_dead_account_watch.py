import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from config.constants import AccountStatus
from core.client_manager import client_manager
from helpers import dead_account
from infra import db
from repositories import account_repo
from services import account_service, account_watch_service


class DeadAccountTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dead_account._account_state_lock = asyncio.Lock()
        dead_account._pending_dead.clear()

    async def asyncTearDown(self) -> None:
        dead_account._pending_dead.clear()

    async def test_database_failure_removes_client_and_retries_pending_mark(self) -> None:
        phone = "+10000000004"
        expected_statuses = (int(AccountStatus.LOGGED_IN),)
        with (
            patch.object(db, "run_db", new=AsyncMock(side_effect=RuntimeError("db down"))),
            patch.object(
                client_manager,
                "remove",
                new=AsyncMock(side_effect=RuntimeError("remove down")),
            ) as remove,
        ):
            marked = await dead_account._mark_and_remove(
                phone,
                "Error",
                "failed",
                expected_statuses=expected_statuses,
            )

        self.assertFalse(marked)
        remove.assert_awaited_once_with(phone)
        self.assertEqual({phone}, dead_account.pending_dead_phones())

        with (
            patch.object(db, "run_db", new=AsyncMock(return_value=0)) as run_db,
            patch.object(client_manager, "remove", new=AsyncMock()) as retry_remove,
        ):
            await dead_account.retry_pending_dead_marks()

        run_db.assert_awaited_once_with(
            account_repo.mark_dead,
            phone,
            "Error",
            "failed",
            expected_statuses,
        )
        retry_remove.assert_not_awaited()
        self.assertEqual(set(), dead_account.pending_dead_phones())


class AccountWatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dead_account._account_state_lock = asyncio.Lock()
        client_manager._clients.clear()
        getattr(account_watch_service, "_probe_fail_streak", {}).clear()

    async def asyncTearDown(self) -> None:
        client_manager._clients.clear()
        getattr(account_watch_service, "_probe_fail_streak", {}).clear()

    async def test_ready_frozen_account_is_marked_dead_before_profile_sync(self) -> None:
        phone = "+10000000034"
        me = SimpleNamespace(
            id=1,
            username="frozen_user",
            bot_verification_icon=5449449325434266744,
        )
        full_user = SimpleNamespace(
            full_user=SimpleNamespace(
                bot_verification=SimpleNamespace(
                    bot_id=777000,
                    description="The account was frozen",
                ),
            ),
        )
        client = AsyncMock(return_value=full_user)
        client.is_connected = lambda: True
        client.get_me.return_value = me
        client_manager._clients[phone] = client

        with (
            patch(
                "core.client_manager.mark_dead_by_reason",
                new=AsyncMock(return_value=True),
            ) as mark_dead,
            patch.object(
                account_watch_service,
                "_sync_profile",
                new=AsyncMock(),
            ) as sync_profile,
        ):
            await account_watch_service._check_one(phone)

        self.assertEqual("GetFullUserRequest", type(client.await_args.args[0]).__name__)
        mark_dead.assert_awaited_once_with(
            phone,
            "AccountFrozen",
            "Telegram账号已冻结(The account was frozen)",
            expected_client=client,
            expected_statuses=None,
        )
        sync_profile.assert_not_awaited()

    async def test_watch_tick_includes_logged_in_and_need_relogin_accounts_outside_pool(self) -> None:
        logged_in = SimpleNamespace(phone="+10000000005", status=int(AccountStatus.LOGGED_IN))
        need_relogin = SimpleNamespace(phone="+10000000006", status=int(AccountStatus.NEED_RELOGIN))
        with (
            patch.object(
                account_watch_service,
                "retry_pending_dead_marks",
                new=AsyncMock(),
            ) as retry_pending,
            patch.object(
                account_watch_service,
                "pending_dead_phones",
                return_value={logged_in.phone},
            ),
            patch.object(
                account_watch_service,
                "run_db",
                new=AsyncMock(return_value=[logged_in, need_relogin]),
            ) as run_db,
            patch.object(
                account_watch_service.client_manager,
                "all_phones",
                return_value=["+10000000007", logged_in.phone],
            ),
            patch.object(account_watch_service, "_check_one", new=AsyncMock()) as check_one,
            patch.object(account_watch_service.random, "uniform", return_value=0),
        ):
            await account_watch_service._watch_tick()

        run_db.assert_awaited_once_with(
            account_repo.find_by_statuses,
            [int(AccountStatus.LOGGED_IN), int(AccountStatus.NEED_RELOGIN)],
        )
        retry_pending.assert_awaited_once_with()
        self.assertEqual(
            [
                call("+10000000007", None),
                call(need_relogin.phone, need_relogin),
            ],
            check_one.await_args_list,
        )

    async def test_network_failure_requires_two_rounds_before_relogin(self) -> None:
        phone = "+10000000008"
        account = SimpleNamespace(phone=phone, status=int(AccountStatus.LOGGED_IN))
        with (
            patch.object(account_watch_service.client_manager, "get_ready_client", return_value=None),
            patch.object(account_watch_service.client_manager, "get_client", return_value=None),
            patch.object(
                account_watch_service.client_manager,
                "connect_account",
                new=AsyncMock(side_effect=RuntimeError("network")),
            ) as connect_account,
            patch.object(account_watch_service, "run_db", new=AsyncMock()) as run_db,
            patch.object(
                account_watch_service.client_manager,
                "remove",
                new=AsyncMock(),
            ) as remove,
            patch.object(
                account_watch_service,
                "mark_dead_and_remove",
                new=AsyncMock(),
            ) as mark_dead,
        ):
            await account_watch_service._check_one(phone, account)

            run_db.assert_not_awaited()
            remove.assert_not_awaited()

            await account_watch_service._check_one(phone, account)

        self.assertEqual(2, connect_account.await_count)
        self.assertEqual(
            [
                call(
                    account_repo.update_status_if_current,
                    phone,
                    int(AccountStatus.NEED_RELOGIN),
                    (int(AccountStatus.LOGGED_IN),),
                ),
            ],
            run_db.await_args_list,
        )
        remove.assert_awaited_once_with(phone)
        mark_dead.assert_not_awaited()

    async def test_successful_probe_clears_network_failure_streak(self) -> None:
        phone = "+10000000035"
        me = SimpleNamespace(
            id=1,
            first_name="first",
            last_name="last",
            username="user",
            bot_verification_icon=None,
        )
        client = SimpleNamespace(
            is_connected=lambda: True,
            get_me=AsyncMock(return_value=me),
        )
        client_manager._clients[phone] = client

        with (
            patch.object(account_watch_service, "run_db", new=AsyncMock()) as run_db,
            patch.object(client_manager, "remove", new=AsyncMock()) as remove,
            patch.object(
                account_watch_service,
                "_sync_profile",
                new=AsyncMock(),
            ) as sync_profile,
        ):
            await account_watch_service._on_ambiguous_fail(
                phone,
                RuntimeError("first network failure"),
                expected_client=client,
                expected_statuses=None,
            )
            await account_watch_service._check_one(phone)
            await account_watch_service._on_ambiguous_fail(
                phone,
                RuntimeError("network failure after success"),
                expected_client=client,
                expected_statuses=None,
            )

        run_db.assert_not_awaited()
        remove.assert_not_awaited()
        sync_profile.assert_awaited_once_with(phone, me)

    async def test_old_ambiguous_failure_does_not_remove_new_authorized_client(self) -> None:
        phone = "+10000000023"
        probe_entered = asyncio.Event()
        release_probe = asyncio.Event()

        async def old_get_me():
            probe_entered.set()
            await release_probe.wait()
            raise RuntimeError("old client network failure")

        def on_event(*_args):
            return lambda handler: handler

        old_client = SimpleNamespace(
            is_connected=lambda: True,
            get_me=old_get_me,
            disconnect=AsyncMock(),
        )
        new_client = SimpleNamespace(
            on=on_event,
            is_connected=lambda: True,
            disconnect=AsyncMock(),
        )
        me = SimpleNamespace(id=1, first_name="first", last_name="last", username="user")
        client_manager._clients[phone] = old_client

        with (
            patch.object(account_watch_service, "run_db", new=AsyncMock()) as watch_run_db,
            patch("core.client_manager.run_db", new=AsyncMock(return_value=1)),
        ):
            probe = asyncio.create_task(account_watch_service._check_one(phone))
            await probe_entered.wait()
            await client_manager.register_ready_client(phone, new_client, me)
            release_probe.set()
            await probe

        watch_run_db.assert_not_awaited()
        self.assertIs(new_client, client_manager.get_client(phone))
        old_client.disconnect.assert_awaited_once_with()
        new_client.disconnect.assert_not_awaited()

    async def test_poolless_offline_makes_old_ambiguous_failure_stale(self) -> None:
        phone = "+10000000024"

        with (
            patch.object(account_service, "run_db", new=AsyncMock(return_value=1)),
            patch.object(
                client_manager,
                "remove",
                new=AsyncMock(wraps=client_manager.remove),
            ) as remove,
            patch.object(account_watch_service, "run_db", new=AsyncMock(return_value=0)) as watch_run_db,
        ):
            await account_service.offline(phone)
            await account_watch_service._on_ambiguous_fail(
                phone,
                RuntimeError("late old failure"),
                expected_client=None,
                expected_statuses=(int(AccountStatus.LOGGED_IN),),
            )
            await account_watch_service._on_ambiguous_fail(
                phone,
                RuntimeError("late old failure"),
                expected_client=None,
                expected_statuses=(int(AccountStatus.LOGGED_IN),),
            )

        watch_run_db.assert_awaited_once_with(
            account_repo.update_status_if_current,
            phone,
            int(AccountStatus.NEED_RELOGIN),
            (int(AccountStatus.LOGGED_IN),),
        )
        remove.assert_awaited_once_with(phone)
        self.assertIsNone(client_manager.get_client(phone))

    async def test_poolless_connect_failure_does_not_override_new_registration(self) -> None:
        phone = "+10000000025"
        account = SimpleNamespace(phone=phone, status=int(AccountStatus.LOGGED_IN))
        connect_entered = asyncio.Event()
        release_connect = asyncio.Event()

        async def old_connect(_account) -> None:
            connect_entered.set()
            await release_connect.wait()
            raise RuntimeError("old poolless reconnect failure")

        def on_event(*_args):
            return lambda handler: handler

        new_client = SimpleNamespace(
            on=on_event,
            is_connected=lambda: True,
            disconnect=AsyncMock(),
        )
        me = SimpleNamespace(id=1, first_name="first", last_name="last", username="user")

        with (
            patch.object(
                client_manager,
                "connect_account",
                new=AsyncMock(side_effect=old_connect),
            ),
            patch.object(account_watch_service, "run_db", new=AsyncMock()) as watch_run_db,
            patch("core.client_manager.run_db", new=AsyncMock(return_value=1)),
        ):
            reconnect = asyncio.create_task(account_watch_service._check_one(phone, account))
            await connect_entered.wait()
            await client_manager.register_ready_client(phone, new_client, me)
            release_connect.set()
            await reconnect

        watch_run_db.assert_not_awaited()
        self.assertIs(new_client, client_manager.get_client(phone))
        new_client.disconnect.assert_not_awaited()

    async def test_offline_makes_old_terminal_failure_stale(self) -> None:
        phone = "+10000000026"
        expected_statuses = (int(AccountStatus.LOGGED_IN),)

        with (
            patch.object(account_service, "run_db", new=AsyncMock(return_value=1)),
            patch.object(
                client_manager,
                "remove",
                new=AsyncMock(wraps=client_manager.remove),
            ) as remove,
            patch("infra.db.run_db", new=AsyncMock(return_value=0)) as dead_run_db,
        ):
            await account_service.offline(phone)
            marked = await dead_account.mark_dead_and_remove(
                phone,
                RuntimeError("late terminal failure"),
                expected_client=None,
                expected_statuses=expected_statuses,
            )

        self.assertFalse(marked)
        dead_run_db.assert_awaited_once_with(
            account_repo.mark_dead,
            phone,
            "RuntimeError",
            "late terminal failure",
            expected_statuses,
        )
        remove.assert_awaited_once_with(phone)
        self.assertIsNone(client_manager.get_client(phone))


if __name__ == "__main__":
    unittest.main()
