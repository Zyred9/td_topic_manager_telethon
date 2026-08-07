import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from config.constants import TaskStatus
from core.client_manager import client_manager
from infra import db
from services import schedule_service


class ScheduleLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        schedule_service._task_state_lock = asyncio.Lock()

    async def asyncTearDown(self) -> None:
        await schedule_service.cancel_all_tasks()

    async def test_quota_full_keeps_target_group(self) -> None:
        phone = "+10000000000"
        with (
            patch.object(
                schedule_service.message_sender,
                "send_text",
                new=AsyncMock(side_effect=[
                    (False, schedule_service.message_sender.REASON_QUOTA_FULL),
                    asyncio.CancelledError(),
                ]),
            ),
            patch.object(schedule_service, "run_db", new=AsyncMock()) as run_db,
        ):
            task = asyncio.create_task(schedule_service._run_loop(phone, [123], "hello", 0))
            schedule_service._tasks[phone] = task
            with self.assertRaises(asyncio.CancelledError):
                await task

        run_db.assert_not_awaited()
        self.assertNotIn(phone, schedule_service._tasks)

    async def test_unhandled_error_retries_next_round(self) -> None:
        phone = "+10000000001"
        with (
            patch.object(
                schedule_service.message_sender,
                "send_text",
                new=AsyncMock(side_effect=[RuntimeError("boom"), asyncio.CancelledError()]),
            ),
            patch.object(schedule_service, "run_db", new=AsyncMock()) as run_db,
        ):
            task = asyncio.create_task(schedule_service._run_loop(phone, [123], "hello", 0))
            schedule_service._tasks[phone] = task
            with self.assertRaises(asyncio.CancelledError):
                await task

        run_db.assert_not_awaited()
        self.assertNotIn(phone, schedule_service._tasks)

    async def test_temporary_offline_retries_next_round(self) -> None:
        phone = "+10000000002"
        with (
            patch.object(
                schedule_service.message_sender,
                "send_text",
                new=AsyncMock(side_effect=[(False, "小号未就绪"), asyncio.CancelledError()]),
            ),
            patch.object(client_manager, "get_ready_client", return_value=None),
            patch.object(
                schedule_service,
                "run_db",
                new=AsyncMock(return_value=SimpleNamespace(is_dead=0)),
            ) as run_db,
        ):
            task = asyncio.create_task(schedule_service._run_loop(phone, [123], "hello", 0))
            schedule_service._tasks[phone] = task
            with self.assertRaises(asyncio.CancelledError):
                await task

        run_db.assert_awaited_once_with(schedule_service.account_repo.find_by_phone, phone)
        self.assertNotIn(phone, schedule_service._tasks)

    async def test_dead_account_stops_and_updates_status(self) -> None:
        phone = "+10000000003"
        with (
            patch.object(
                schedule_service.message_sender,
                "send_text",
                new=AsyncMock(return_value=(False, "账号已失效")),
            ),
            patch.object(client_manager, "get_ready_client", return_value=None),
            patch.object(
                schedule_service,
                "run_db",
                new=AsyncMock(side_effect=[SimpleNamespace(is_dead=1), None]),
            ) as run_db,
        ):
            task = asyncio.create_task(schedule_service._run_loop(phone, [123], "hello", 0))
            schedule_service._tasks[phone] = task
            await task

        self.assertEqual(
            run_db.await_args_list,
            [
                call(schedule_service.account_repo.find_by_phone, phone),
                call(schedule_service.schedule_repo.update_status, phone, int(TaskStatus.STOPPED)),
            ],
        )
        self.assertNotIn(phone, schedule_service._tasks)

    async def test_start_rejects_unready_account_before_persisting(self) -> None:
        with (
            patch.object(client_manager, "get_ready_client", return_value=None),
            patch.object(schedule_service, "run_db", new=AsyncMock()) as run_db,
        ):
            with self.assertRaisesRegex(ValueError, "小号未就绪"):
                await schedule_service.start("+10000000004", [123], "hello", 1)

        run_db.assert_not_awaited()

    async def test_start_rejects_blank_content_before_persisting(self) -> None:
        with patch.object(schedule_service, "run_db", new=AsyncMock()) as run_db:
            with self.assertRaisesRegex(ValueError, "发送内容不能为空"):
                await schedule_service.start("+10000000004", [123], "  ", 1)

        run_db.assert_not_awaited()

    async def test_stop_keeps_task_when_status_update_fails(self) -> None:
        phone = "+10000000005"
        task = asyncio.create_task(asyncio.sleep(60))
        schedule_service._tasks[phone] = task
        with patch.object(
            schedule_service,
            "run_db",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                await schedule_service.stop(phone)

        self.assertIs(schedule_service._tasks[phone], task)
        self.assertFalse(task.cancelled())

    async def test_concurrent_starts_are_serialized(self) -> None:
        active = 0
        max_active = 0

        async def fake_start(*_args) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1

        with patch.object(schedule_service, "_start_locked", side_effect=fake_start):
            await asyncio.gather(
                schedule_service.start("+10000000006", [123], "hello", 1),
                schedule_service.start("+10000000006", [123], "hello", 1),
            )

        self.assertEqual(max_active, 1)


class ScheduleMigrationTest(unittest.TestCase):
    def test_old_schedule_table_gets_interval_sec_column(self) -> None:
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection

        def column_exists(_cursor, table: str, column: str) -> bool:
            return (table, column) != ("t_schedule_task", "interval_sec")

        with (
            patch.object(db, "get_connection", return_value=connection_context),
            patch.object(db, "_column_exists", side_effect=column_exists),
            patch.object(db, "_index_exists", return_value=True),
        ):
            db._run_migrations()

        cursor.execute.assert_called_once_with(
            "ALTER TABLE t_schedule_task ADD COLUMN interval_sec INT NOT NULL DEFAULT 0 "
            "COMMENT '发送间隔(秒),与 interval_min 叠加' AFTER interval_min"
        )


if __name__ == "__main__":
    unittest.main()
