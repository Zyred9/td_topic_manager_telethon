import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors

from config.constants import SendSource
from core import message_sender
from core.throttle import throttle
from services import group_op_service


class _Client:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls = 0
        self.error = error

    async def send_message(self, chat_id, text, reply_to=None) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        await asyncio.sleep(0.01)


class MessageSenderTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_phone_concurrent_send_respects_quota(self) -> None:
        phone = "+10000000002"
        client = _Client()
        throttle.clear_phone(phone)
        settings = SimpleNamespace(risk=SimpleNamespace(
            min_interval_ms=0,
            max_per_min_per_phone=1,
        ))

        with (
            patch("core.throttle.get_settings", return_value=settings),
            patch.object(message_sender.client_manager, "get_ready_client", return_value=client),
            patch.object(message_sender, "_log_send", new=AsyncMock()),
        ):
            results = await asyncio.gather(
                message_sender.send_text(phone, 1, "a"),
                message_sender.send_text(phone, 1, "b"),
            )

        self.assertEqual(1, client.calls)
        self.assertEqual([(True, ""), (False, message_sender.REASON_QUOTA_FULL)], results)
        throttle.clear_phone(phone)

    async def test_user_banned_in_channel_does_not_mark_account_dead(self) -> None:
        phone = "+10000000009"
        client = _Client(errors.UserBannedInChannelError(None))
        throttle.clear_phone(phone)
        settings = SimpleNamespace(risk=SimpleNamespace(
            min_interval_ms=0,
            max_per_min_per_phone=8,
        ))

        with (
            patch("core.throttle.get_settings", return_value=settings),
            patch.object(message_sender.client_manager, "get_ready_client", return_value=client),
            patch.object(message_sender, "_log_send", new=AsyncMock()),
            patch.object(
                message_sender,
                "mark_dead_and_remove",
                new=AsyncMock(),
            ) as mark_dead,
        ):
            ok, reason = await message_sender.send_text(phone, 1, "hello")

        self.assertFalse(ok)
        self.assertEqual(1, client.calls)
        self.assertNotIn("账号已失效", reason)
        mark_dead.assert_not_awaited()
        throttle.clear_phone(phone)

    async def test_terminal_session_error_still_marks_account_dead(self) -> None:
        phone = "+10000000010"
        error = errors.AuthKeyUnregisteredError(None)
        client = _Client(error)
        throttle.clear_phone(phone)
        settings = SimpleNamespace(risk=SimpleNamespace(
            min_interval_ms=0,
            max_per_min_per_phone=8,
        ))

        with (
            patch("core.throttle.get_settings", return_value=settings),
            patch.object(message_sender.client_manager, "get_ready_client", return_value=client),
            patch.object(message_sender, "_log_send", new=AsyncMock()),
            patch.object(
                message_sender,
                "mark_dead_and_remove",
                new=AsyncMock(),
            ) as mark_dead,
        ):
            ok, reason = await message_sender.send_text(phone, 1, "hello")

        self.assertFalse(ok)
        self.assertIn("账号已失效", reason)
        mark_dead.assert_awaited_once_with(phone, error, expected_client=client)
        throttle.clear_phone(phone)


class BroadcastTest(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_uses_unified_sender_and_keeps_reason(self) -> None:
        phone = "+10000000003"
        with (
            patch.object(group_op_service.asyncio, "sleep", new=AsyncMock()),
            patch.object(
                group_op_service,
                "send_text",
                new=AsyncMock(return_value=(False, message_sender.REASON_QUOTA_FULL)),
            ) as send_text,
            patch.object(group_op_service.batch_store, "set_item") as set_item,
            patch.object(group_op_service.batch_store, "finish") as finish,
        ):
            await group_op_service.run_broadcast([phone], 123, "hello", "batch-1")

        send_text.assert_awaited_once_with(
            phone, 123, "hello", source=SendSource.SCHEDULE,
        )
        set_item.assert_called_once_with(
            "batch-1",
            phone,
            group_op_service.ITEM_FAILED,
            fail_reason=message_sender.REASON_QUOTA_FULL,
        )
        finish.assert_called_once_with("batch-1")


if __name__ == "__main__":
    unittest.main()
