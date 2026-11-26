"""Bounded, loop-free bidirectional text clipboard synchronization."""

import hashlib
import threading

from .protocol import MAX_CLIPBOARD_BYTES, ProtocolError

CLIPBOARD_MODES = ("auto", "manual", "off")


def text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_clipboard_payload(message):
    text = message.get("text")
    if not isinstance(text, str):
        raise ProtocolError("无效剪贴板内容")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_CLIPBOARD_BYTES:
        raise ProtocolError("剪贴板文本超出 128 KiB 上限")
    digest = message.get("sha256")
    expected = hashlib.sha256(encoded).hexdigest()
    if digest is not None and (not isinstance(digest, str) or digest != expected):
        raise ProtocolError("剪贴板校验失败")
    return text, expected


def build_clipboard_message(text):
    if not isinstance(text, str) or not text:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_CLIPBOARD_BYTES:
        return None
    return {
        "type": "clipboard",
        "text": text,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


class ClipboardGuard:
    """Prevents ping-pong clipboard echo loops between local and remote machines."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_remote_hash = ""
        self._last_local_hash = ""

    def accept_remote(self, text):
        """Record remote text arrival. Returns True if text is new and should be written locally."""
        if not isinstance(text, str):
            return False
        digest = text_digest(text)
        with self._lock:
            if digest == self._last_remote_hash:
                return False
            self._last_remote_hash = digest
            self._last_local_hash = digest
            return True

    def should_send_local(self, text, force=False):
        """Decide whether a local clipboard change should be sent to remote."""
        if not isinstance(text, str) or not text:
            return False
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            return False
        digest = text_digest(text)
        with self._lock:
            if not force and digest in (self._last_remote_hash, self._last_local_hash):
                return False
            self._last_local_hash = digest
            return True

    def reset(self):
        with self._lock:
            self._last_remote_hash = ""
            self._last_local_hash = ""
