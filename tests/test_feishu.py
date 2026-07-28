from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_message.channels.feishu import FeishuGateway, parse_receive_event

from tests.helpers import make_config


class FeishuParsingTests(unittest.TestCase):
    def test_gateway_defers_sdk_import_until_websocket_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {
                "AGENT_MESSAGE_FEISHU_APP_ID": "cli_test",
                "AGENT_MESSAGE_FEISHU_APP_SECRET": "test_secret",
            },
            clear=False,
        ):
            gateway = FeishuGateway(make_config(Path(temp)), lambda _message: None)
            self.assertIsNone(gateway._lark)
            self.assertIsNone(gateway._client)

    def test_accepts_only_p2p_text(self) -> None:
        event = {
            "header": {"event_id": "event-1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou-1"}},
                "message": {
                    "message_id": "msg-1",
                    "chat_id": "chat-1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"/status"}',
                },
            },
        }
        parsed = parse_receive_event(event)
        assert parsed is not None
        self.assertEqual(parsed.text, "/status")
        event["event"]["message"]["chat_type"] = "group"
        self.assertIsNone(parse_receive_event(event))
