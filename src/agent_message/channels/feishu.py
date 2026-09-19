"""Feishu event parsing and long-connection gateway."""

from __future__ import annotations

import json
import logging
import threading
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import AppConfig
from ..core.models import InboundMessage

LOGGER = logging.getLogger(__name__)

_SUPPORTED_MESSAGE_TYPES = {"text", "image", "file"}

# Feishu im.v1.file.create file_type values; anything else uploads as "stream".
_FEISHU_FILE_TYPES = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}


class FeishuError(RuntimeError):
    pass


def _describe(response: Any) -> str:
    """Render a lark response with its API code and message instead of a repr."""
    code = getattr(response, "code", None)
    message = getattr(response, "msg", None)
    if code is None and message is None:
        return repr(response)
    return f"code={code} msg={message}"


def parse_receive_event(data: Any) -> InboundMessage | None:
    """Extract a p2p text/image/file message from an im.message.receive_v1 event.

    The function accepts both lark-oapi model objects and plain dict fixtures.
    """

    raw = _to_dict(data)
    event = raw.get("event", raw)
    header = raw.get("header", {})
    message = event.get("message", {}) if isinstance(event, dict) else {}
    sender = event.get("sender", {}) if isinstance(event, dict) else {}
    sender_id = sender.get("sender_id", {}) if isinstance(sender, dict) else {}
    if not isinstance(message, dict) or message.get("chat_type") != "p2p":
        return None
    message_type = message.get("message_type")
    if message_type not in _SUPPORTED_MESSAGE_TYPES:
        return None
    content = message.get("content")
    try:
        payload = json.loads(content) if isinstance(content, str) else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    text = ""
    file_key: str | None = None
    file_name: str | None = None
    if message_type == "text":
        text = payload.get("text", "")
        if not isinstance(text, str) or not text:
            return None
    elif message_type == "image":
        file_key = payload.get("image_key")
        if not isinstance(file_key, str) or not file_key:
            return None
    else:
        file_key = payload.get("file_key")
        name = payload.get("file_name")
        if not isinstance(file_key, str) or not file_key:
            return None
        if name is not None and not isinstance(name, str):
            return None
        file_name = name
    event_id = header.get("event_id") if isinstance(header, dict) else None
    message_id = message.get("message_id")
    chat_id = message.get("chat_id")
    open_id = sender_id.get("open_id") if isinstance(sender_id, dict) else None
    if not all(isinstance(value, str) and value for value in (event_id, message_id, chat_id, open_id)):
        return None
    return InboundMessage(
        event_id,
        message_id,
        chat_id,
        "p2p",
        open_id,
        text,
        message_type=message_type,
        file_key=file_key,
        file_name=file_name,
    )


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

    def _require_ready(self) -> tuple[Any, Any]:
        if not self._ready.wait(timeout=15) or self._lark is None or self._client is None:
            raise FeishuError("Feishu WebSocket SDK is not ready")
        return self._lark, self._client

    def _send_message(self, chat_id: str, msg_type: str, payload: dict[str, Any]) -> None:
        lark, client = self._require_ready()
        body = (
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type(msg_type)
            .content(json.dumps(payload, ensure_ascii=False))
            .build()
        )
        request = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = client.im.v1.message.create(request)
        if not getattr(response, "success", lambda: False)():
            raise FeishuError(f"send message failed: {_describe(response)}")

    def send_text(self, chat_id: str, content: str) -> None:
        self._send_message(chat_id, "text", {"text": content})

    def send_media(self, chat_id: str, kind: str, path: Path) -> None:
        if kind == "image":
            self.send_image(chat_id, path)
        elif kind == "file":
            self.send_file(chat_id, path)
        else:
            raise FeishuError(f"unknown media kind: {kind}")

    def send_image(self, chat_id: str, path: Path) -> None:
        lark, client = self._require_ready()
        with open(path, "rb") as stream:
            body = (
                lark.im.v1.CreateImageRequestBody.builder()
                .image_type("message")
                .image(stream)
                .build()
            )
            request = lark.im.v1.CreateImageRequest.builder().request_body(body).build()
            response = client.im.v1.image.create(request)
        if not getattr(response, "success", lambda: False)():
            raise FeishuError(f"upload image failed: {_describe(response)}")
        image_key = getattr(getattr(response, "data", None), "image_key", None)
        if not image_key:
            raise FeishuError(f"upload image returned no image_key: {_describe(response)}")
        self._send_message(chat_id, "image", {"image_key": image_key})

    def send_file(self, chat_id: str, path: Path) -> None:
        lark, client = self._require_ready()
        file_type = _FEISHU_FILE_TYPES.get(path.suffix.lower(), "stream")
        with open(path, "rb") as stream:
            body = (
                lark.im.v1.CreateFileRequestBody.builder()
                .file_type(file_type)
                .file_name(path.name)
                .file(stream)
                .build()
            )
            request = lark.im.v1.CreateFileRequest.builder().request_body(body).build()
            response = client.im.v1.file.create(request)
        if not getattr(response, "success", lambda: False)():
            raise FeishuError(f"upload file failed: {_describe(response)}")
        file_key = getattr(getattr(response, "data", None), "file_key", None)
        if not file_key:
            raise FeishuError(f"upload file returned no file_key: {_describe(response)}")
        self._send_message(chat_id, "file", {"file_key": file_key})

    def download_resource(
        self,
        message_id: str,
        file_key: str,
        kind: str,
        dest: Path,
        max_bytes: int,
    ) -> None:
        """Download an inbound image/file resource to dest, enforcing max_bytes."""
        lark, client = self._require_ready()
        request = (
            lark.im.v1.GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(kind)
            .build()
        )
        response = client.im.v1.message_resource.get(request)
        if not getattr(response, "success", lambda: False)():
            raise FeishuError(f"download resource failed: {_describe(response)}")
        stream = getattr(response, "file", None)
        if stream is None:
            raise FeishuError("download resource returned no file stream")
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with open(dest, "wb") as output:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise FeishuError(
                            f"attachment exceeds size limit ({max_bytes // (1024 * 1024)}MB)"
                        )
                    output.write(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            raise

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
