from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_message.orchestration.service import _MAX_SEND_ATTEMPTS, BridgeService

from tests.helpers import make_config


class OutboxFailurePolicyTests(unittest.TestCase):
    """A permanently failing entry must not block every later reply."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temp.name), ("alpha",))
        self.service = BridgeService(self.config, lambda _chat, _text: None)

    def tearDown(self) -> None:
        self.service.state.close()
        self.temp.cleanup()

    def rows(self) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in self.service.state._connection.execute(
                "SELECT id, kind, status, attempts, content FROM outbox ORDER BY id"
            ).fetchall()
        ]

    def fail_repeatedly(self, entry, times: int) -> int:
        attempts = 0
        for _ in range(times):
            attempts = self.service.state.mark_outbox_failed(entry.id, "boom")
            self.service._give_up(entry, attempts, "boom")
        return attempts

    def test_attachment_is_dropped_with_a_notice_after_the_attempt_cap(self) -> None:
        image = self.config.projects["alpha"].path / "plot.png"
        image.write_bytes(b"png")
        self.service.state.enqueue_outbox(
            "chat-1", "图片：plot.png", kind="image", file_path=str(image)
        )
        entry = self.service.state.next_outbox()
        assert entry is not None

        for _ in range(_MAX_SEND_ATTEMPTS - 1):
            attempts = self.service.state.mark_outbox_failed(entry.id, "boom")
            self.assertFalse(self.service._give_up(entry, attempts, "boom"))
            self.assertEqual(self.rows()[0]["status"], "pending")

        attempts = self.service.state.mark_outbox_failed(entry.id, "boom")
        self.assertTrue(self.service._give_up(entry, attempts, "boom"))

        rows = self.rows()
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["attempts"], _MAX_SEND_ATTEMPTS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["status"], "pending")
        self.assertEqual(rows[1]["kind"], "text")
        self.assertIn("已跳过", str(rows[1]["content"]))
        self.assertIn("boom", str(rows[1]["content"]))

    def test_text_entry_is_dropped_without_a_notice_loop(self) -> None:
        self.service.state.enqueue_outbox("chat-1", "hello")
        entry = self.service.state.next_outbox()
        assert entry is not None

        attempts = self.fail_repeatedly(entry, _MAX_SEND_ATTEMPTS)

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(attempts, _MAX_SEND_ATTEMPTS)

    def test_terminal_entry_no_longer_holds_up_the_queue(self) -> None:
        self.service.state.enqueue_outbox("chat-1", "poison")
        self.service.state.enqueue_outbox("chat-1", "next reply")
        entry = self.service.state.next_outbox()
        assert entry is not None
        self.fail_repeatedly(entry, _MAX_SEND_ATTEMPTS)

        following = self.service.state.next_outbox()

        assert following is not None
        self.assertEqual(following.content, "next reply")


if __name__ == "__main__":
    unittest.main()
