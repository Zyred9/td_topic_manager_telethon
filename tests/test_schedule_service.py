import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from config.constants import TaskStatus
from services import schedule_service


class ScheduleLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        schedule_service._tasks.clear()

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

    async def test_unhandled_error_stops_and_removes_task(self) -> None:
        phone = "+10000000001"
        with (
            patch.object(
                schedule_service.message_sender,
                "send_text",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(schedule_service, "run_db", new=AsyncMock()) as run_db,
        ):
            task = asyncio.create_task(schedule_service._run_loop(phone, [123], "hello", 0))
            schedule_service._tasks[phone] = task
            await task

        run_db.assert_awaited_once_with(
            schedule_service.schedule_repo.update_status,
            phone,
            int(TaskStatus.STOPPED),
        )
        self.assertNotIn(phone, schedule_service._tasks)


if __name__ == "__main__":
    unittest.main()
