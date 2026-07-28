from __future__ import annotations

import json
import logging
import threading
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .models import InboundMessage

LOGGER = logging.getLogger(__name__)


class FeishuError(RuntimeError):
    pass


def parse_receive_event(data: Any) -> InboundMessage | None:
    """Extract only p2p text from an im.message.receive_v1 event.

    The function accepts both lark-oapi model objects and plain dict fixtures.
    """

    raw = _to_dict(data)
    event = raw.get("event", raw)
    header = raw.get("header", {})
    message = event.get("message", {}) if isinstance(event, dict) else {}
    sender = event.get("sender", {}) if isinstance(event, dict) else {}
    sender_id = sender.get("sender_id", {}) if isinstance(sender, dict) else {}
    if not isinstance(message, dict) or message.get("chat_type") != "p2p" or message.get("message_type") != "text":
        return None
    content = message.get("content")
    try:
        text = json.loads(content).get("text", "") if isinstance(content, str) else ""
    except json.JSONDecodeError:
        return None
    event_id = header.get("event_id") if isinstance(header, dict) else None
    message_id = message.get("message_id")
    chat_id = message.get("chat_id")
    open_id = sender_id.get("open_id") if isinstance(sender_id, dict) else None
    if not all(isinstance(value, str) and value for value in (event_id, message_id, chat_id, open_id, text)):
        return None
    return InboundMessage(event_id, message_id, chat_id, "p2p", open_id, text)


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    # lark.JSON.marshal is intentionally not used here so unit tests never require lark-oapi.
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        return converted if isinstance(converted, dict) else {}
    if hasattr(value, "__dict__"):
        return _object_to_dict(value)
    return {}


def _object_to_dict(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in vars(value).items():
        if key.startswith("_"):
            continue
        if isinstance(item, list):
            result[key] = [_object_to_dict(member) if hasattr(member, "__dict__") else member for member in item]
        elif hasattr(item, "__dict__"):
            result[key] = _object_to_dict(item)
        else:
            result[key] = item
    return result


@dataclass
class FeishuGateway:
    config: AppConfig
    on_message: Callable[[InboundMessage], None]

    def __post_init__(self) -> None:
        if not self.config.app_id or not self.config.app_secret:
            raise FeishuError(
                "missing AGENT_MESSAGE_FEISHU_APP_ID or AGENT_MESSAGE_FEISHU_APP_SECRET"
            )
        self._lark: Any | None = None
        self._client: Any | None = None
        self._ready = threading.Event()

    def send_text(self, chat_id: str, content: str) -> None:
        if not self._ready.wait(timeout=15) or self._lark is None or self._client is None:
            raise FeishuError("Feishu WebSocket SDK is not ready")
        body = (
            self._lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": content}, ensure_ascii=False))
            .build()
        )
        request = (
            self._lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not getattr(response, "success", lambda: False)():
            raise FeishuError(f"send message failed: {response}")

    def start_in_thread(self) -> threading.Thread:
        def callback(data: Any) -> None:
            inbound = parse_receive_event(data)
            if inbound:
                self.on_message(inbound)

        def start() -> None:
            # lark-oapi/ws/client.py stores a module-global event loop at import time.
            # Importing and constructing it here guarantees that its blocking `start()`
            # owns this thread's loop, not the bridge's asyncio worker loop.
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            try:
                import lark_oapi as lark  # type: ignore[import-not-found]
                import lark_oapi.ws.client as lark_ws_client  # type: ignore[import-not-found]
            except ImportError as exc:
                LOGGER.exception("lark-oapi is not installed; run `uv sync` first")
                raise FeishuError("lark-oapi is not installed; run `uv sync` first") from exc

            # Defensive reset for environments that imported lark-oapi before this thread.
            lark_ws_client.loop = ws_loop
            self._lark = lark
            self._client = lark.Client.builder().app_id(self.config.app_id).app_secret(self.config.app_secret).build()
            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(callback)
                .build()
            )
            ws_client = lark.ws.Client(
                self.config.app_id,
                self.config.app_secret,
                event_handler=event_handler,
            )
            self._ready.set()
            try:
                ws_client.start()
            finally:
                ws_loop.close()

        thread = threading.Thread(target=start, name="feishu-long-connection", daemon=True)
        thread.start()
        return thread
