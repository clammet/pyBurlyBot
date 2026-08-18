"""Minimal synchronous WebSocket (RFC 6455) client for worker-thread use.

Deliberately small, in the spirit of util/http.py: plain ws:// only (the one
consumer talks to a service on a private docker network), text messages,
automatic ping/pong, fragmented-message reassembly, and a hard cap on
assembled message size. No extensions are offered or accepted.
"""

from base64 import b64encode
from hashlib import sha1
from os import urandom
from socket import create_connection, socket
from struct import pack, unpack
from urllib.parse import urlsplit


_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER_BYTES = 16 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 4 * 1024 * 1024

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    """The server closed the connection."""


def _mask(payload: bytes) -> bytes:
    key = urandom(4)
    return key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _frame(opcode: int, payload: bytes) -> bytes:
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + pack(">H", length)
    else:
        header += bytes([0x80 | 127]) + pack(">Q", length)
    return header + _mask(payload)


class WebSocketClient:
    """One plaintext WebSocket connection. Not thread-safe: use from a single
    worker thread (the intended consumers open one connection per request)."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws":
            raise WebSocketError("Only ws:// URLs are supported (got %s)" % url)
        if not parsed.hostname:
            raise WebSocketError("URL has no hostname: %s" % url)
        self.max_message_bytes = max_message_bytes
        self._buffer = b""
        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            self._sock: socket | None = create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise WebSocketError(
                "Could not connect to %s:%d: %s" % (host, port, exc)
            ) from exc
        try:
            self._handshake(host, port, path, headers or {})
        except BaseException:
            self.close()
            raise

    def _handshake(
        self, host: str, port: int, path: str, headers: dict[str, str]
    ) -> None:
        sock = self._require_sock()
        key = b64encode(urandom(16)).decode()
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s:%d" % (host, port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        lines.extend("%s: %s" % item for item in headers.items())
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        response = b""
        while b"\r\n\r\n" not in response:
            if len(response) > _MAX_HEADER_BYTES:
                raise WebSocketError("Oversized handshake response")
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("Connection closed during handshake")
            response += chunk
        head, _, extra = response.partition(b"\r\n\r\n")
        self._buffer = extra  # frame bytes the server sent along already
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            raise WebSocketError("Handshake rejected: %s" % status_line)
        expected = b64encode(sha1((key + _ACCEPT_GUID).encode()).digest()).decode()  # noqa: S324 - RFC 6455 handshake constant, not authentication
        accept = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in header_lines)
        }.get("sec-websocket-accept")
        if accept != expected:
            raise WebSocketError("Handshake accept mismatch")

    def _require_sock(self) -> socket:
        if self._sock is None:
            raise WebSocketClosed("Connection is closed")
        return self._sock

    def _read_exact(self, count: int) -> bytes:
        sock = self._require_sock()
        while len(self._buffer) < count:
            chunk = sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("Connection closed mid-frame")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def settimeout(self, timeout: float | None) -> None:
        self._require_sock().settimeout(timeout)

    def send_text(self, text: str) -> None:
        self._require_sock().sendall(_frame(_OP_TEXT, text.encode()))

    def recv_text(self) -> str:
        """Return the next complete text message; answers pings internally."""
        message = b""
        expecting_continuation = False
        while True:
            first, second = self._read_exact(2)
            fin, opcode = first & 0x80, first & 0x0F
            length = second & 0x7F
            if length == 126:
                (length,) = unpack(">H", self._read_exact(2))
            elif length == 127:
                (length,) = unpack(">Q", self._read_exact(8))
            if length + len(message) > self.max_message_bytes:
                raise WebSocketError(
                    "Message exceeds %d bytes" % self.max_message_bytes
                )
            payload = self._read_exact(length) if length else b""

            if opcode == _OP_PING:
                self._require_sock().sendall(_frame(_OP_PONG, payload))
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                self.close()
                raise WebSocketClosed("Server closed the connection")
            if opcode in (_OP_TEXT, _OP_BINARY):
                if expecting_continuation:
                    raise WebSocketError("Interleaved message frames")
                message += payload
                expecting_continuation = not fin
            elif opcode == _OP_CONTINUATION:
                if not expecting_continuation:
                    raise WebSocketError("Unexpected continuation frame")
                message += payload
                expecting_continuation = not fin
            else:
                raise WebSocketError("Unsupported frame opcode %d" % opcode)
            if not expecting_continuation:
                return message.decode("utf-8", "replace")

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.sendall(_frame(_OP_CLOSE, b""))
            except OSError:
                pass
            sock.close()

    def __enter__(self) -> "WebSocketClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
