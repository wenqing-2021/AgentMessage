from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_message.channels.feishu import FeishuError, FeishuGateway, parse_receive_event

from tests.helpers import make_config


def _event(message: dict, event_id: str = "event-1") -> dict:
    return {
        "header": {"event_id": event_id},
        "event": {
            "sender": {"sender_id": {"open_id": "ou-1"}},
            "message": message,
        },
    }


def _p2p_message(message_type: str, content: str) -> dict:
    return {
        "message_id": "msg-1",
        "chat_id": "chat-1",
        "chat_type": "p2p",
        "message_type": message_type,
        "content": content,
    }


def _ready_gateway(root: Path) -> tuple[FeishuGateway, MagicMock]:
    with patch.dict(
        os.environ,
        {
            "AGENT_MESSAGE_FEISHU_APP_ID": "cli_test",
            "AGENT_MESSAGE_FEISHU_APP_SECRET": "test_secret",
        },
        clear=False,
    ):
        gateway = FeishuGateway(make_config(root), lambda _message: None)
    client = MagicMock()
    gateway._lark = MagicMock()
    gateway._client = client
    gateway._ready.set()
    return gateway, client


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

    def test_accepts_p2p_image_message(self) -> None:
        parsed = parse_receive_event(
            _event(_p2p_message("image", '{"image_key": "img-1"}'))
        )
        assert parsed is not None
        self.assertEqual(parsed.message_type, "image")
        self.assertEqual(parsed.file_key, "img-1")
        self.assertIsNone(parsed.file_name)
        self.assertEqual(parsed.text, "")

    def test_accepts_p2p_file_message(self) -> None:
        parsed = parse_receive_event(
            _event(_p2p_message("file", '{"file_key": "fk-1", "file_name": "report.pdf"}'))
        )
        assert parsed is not None
        self.assertEqual(parsed.message_type, "file")
        self.assertEqual(parsed.file_key, "fk-1")
        self.assertEqual(parsed.file_name, "report.pdf")

    def test_rejects_attachment_with_missing_key_or_group_chat(self) -> None:
        self.assertIsNone(parse_receive_event(_event(_p2p_message("image", "{}"))))
        self.assertIsNone(
            parse_receive_event(_event(_p2p_message("file", '{"file_key": ""}')))
        )
        group = _p2p_message("image", '{"image_key": "img-1"}')
        group["chat_type"] = "group"
        self.assertIsNone(parse_receive_event(_event(group)))
        self.assertIsNone(
            parse_receive_event(_event(_p2p_message("audio", '{"file_key": "fk"}')))
        )


class FeishuMediaGatewayTests(unittest.TestCase):
    def test_send_image_uploads_then_sends_image_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            gateway, client = _ready_gateway(Path(temp))
            image_path = Path(temp) / "plot.png"
            image_path.write_bytes(b"png")
            client.im.v1.image.create.return_value.success.return_value = True
            client.im.v1.image.create.return_value.data.image_key = "img-9"
            client.im.v1.message.create.return_value.success.return_value = True

            gateway.send_media("chat-1", "image", image_path)

            client.im.v1.image.create.assert_called_once()
            client.im.v1.message.create.assert_called_once()

    def test_send_file_uploads_then_sends_file_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            gateway, client = _ready_gateway(Path(temp))
            doc_path = Path(temp) / "report.pdf"
            doc_path.write_bytes(b"pdf")
            client.im.v1.file.create.return_value.success.return_value = True
            client.im.v1.file.create.return_value.data.file_key = "fk-9"
            client.im.v1.message.create.return_value.success.return_value = True

            gateway.send_media("chat-1", "file", doc_path)

            client.im.v1.file.create.assert_called_once()
            client.im.v1.message.create.assert_called_once()

    def test_upload_failure_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            gateway, client = _ready_gateway(Path(temp))
            image_path = Path(temp) / "plot.png"
            image_path.write_bytes(b"png")
            client.im.v1.image.create.return_value.success.return_value = False

            with self.assertRaises(FeishuError):
                gateway.send_image("chat-1", image_path)
            client.im.v1.message.create.assert_not_called()

    def test_download_resource_writes_file_and_enforces_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            gateway, client = _ready_gateway(Path(temp))
            response = client.im.v1.message_resource.get.return_value
            response.success.return_value = True
            response.file = io.BytesIO(b"hello")
            dest = Path(temp) / "inbox" / "a.png"

            gateway.download_resource("m1", "img-1", "image", dest, 10)
            self.assertEqual(dest.read_bytes(), b"hello")

            response.file = io.BytesIO(b"hello world")
            with self.assertRaises(FeishuError):
                gateway.download_resource("m1", "img-1", "image", dest, 5)
            self.assertFalse(dest.exists())
