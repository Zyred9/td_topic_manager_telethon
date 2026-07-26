import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.constants import SendSource
from core import message_sender
from core.throttle import throttle
from services import group_op_service


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, chat_id, text, reply_to=None) -> None:
        self.calls += 1
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
