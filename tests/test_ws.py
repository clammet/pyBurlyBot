import socket
import struct
import threading
from base64 import b64encode
from collections.abc import Callable
from hashlib import sha1
from unittest import TestCase

from util.ws import WebSocketClient, WebSocketClosed, WebSocketError


ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    header = bytes([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack(">H", length)
    else:
        header += bytes([127]) + struct.pack(">Q", length)
    return header + payload


def read_client_frame(conn: socket.socket) -> tuple[int, bytes]:
    def exact(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise ConnectionError("client closed")
            data += chunk
        return data

    first, second = exact(2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", exact(2))
    elif length == 127:
        (length,) = struct.unpack(">Q", exact(8))
    assert second & 0x80, "client frames must be masked"
    mask = exact(4)
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(length)))
    return opcode, payload


class WSServer:
    """One-shot localhost WebSocket server driven by a per-test script."""

    def __init__(
        self, script: Callable[[socket.socket], None], accept: str | None = None
    ) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.accept_override = accept
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(script,), daemon=True)
        self.thread.start()

    def _run(self, script: Callable[[socket.socket], None]) -> None:
        try:
            conn, _addr = self.listener.accept()
            conn.settimeout(5)
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = ""
            for line in request.decode("latin-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = (
                self.accept_override
                or b64encode(
                    sha1((key + ACCEPT_GUID).encode()).digest()  # noqa: S324 - RFC 6455 handshake constant
                ).decode()
            )
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept
                ).encode()
            )
            script(conn)
            conn.close()
        except BaseException as exc:  # noqa: BLE001 - surfaced via self.error in join()
            self.error = exc
        finally:
            self.listener.close()

    def join(self) -> None:
        self.thread.join(timeout=5)
        if self.error is not None:
            raise self.error


class WebSocketClientTest(TestCase):
    def connect(self, server: WSServer, **kwargs: object) -> WebSocketClient:
        client = WebSocketClient("ws://127.0.0.1:%d" % server.port, timeout=5, **kwargs)  # type: ignore[arg-type]
        self.addCleanup(client.close)
        return client

    def test_text_roundtrip_and_auth_header(self) -> None:
        seen: dict[str, object] = {}

        def script(conn: socket.socket) -> None:
            opcode, payload = read_client_frame(conn)
            seen["client"] = (opcode, payload)
            conn.sendall(server_frame(0x1, "pong text é".encode()))

        server = WSServer(script)
        client = self.connect(server, headers={"Authorization": "Bearer tok"})
        client.send_text("hello")
        self.assertEqual(client.recv_text(), "pong text é")
        server.join()
        self.assertEqual(seen["client"], (0x1, b"hello"))

    def test_fragmented_message_reassembly(self) -> None:
        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x1, b"first ", fin=False))
            conn.sendall(server_frame(0x0, b"second", fin=True))

        server = WSServer(script)
        self.assertEqual(self.connect(server).recv_text(), "first second")
        server.join()

    def test_ping_is_answered_automatically(self) -> None:
        seen: dict[str, object] = {}

        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x9, b"marco"))
            seen["pong"] = read_client_frame(conn)
            conn.sendall(server_frame(0x1, b"done"))

        server = WSServer(script)
        self.assertEqual(self.connect(server).recv_text(), "done")
        server.join()
        self.assertEqual(seen["pong"], (0xA, b"marco"))

    def test_sixteen_bit_length(self) -> None:
        payload = b"x" * 60000

        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x1, payload))

        server = WSServer(script)
        self.assertEqual(self.connect(server).recv_text(), payload.decode())
        server.join()

    def test_server_close_raises(self) -> None:
        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x8, b""))

        server = WSServer(script)
        with self.assertRaises(WebSocketClosed):
            self.connect(server).recv_text()

    def test_message_size_cap(self) -> None:
        def script(conn: socket.socket) -> None:
            conn.sendall(server_frame(0x1, b"y" * 300))

        server = WSServer(script)
        client = self.connect(server, max_message_bytes=100)
        with self.assertRaises(WebSocketError):
            client.recv_text()

    def test_bad_accept_header_is_rejected(self) -> None:
        server = WSServer(lambda conn: None, accept="bm90IHRoZSByaWdodCBhY2NlcHQ=")
        with self.assertRaises(WebSocketError):
            self.connect(server)

    def test_rejects_non_ws_urls(self) -> None:
        for url in ("wss://example.test/", "http://example.test/"):
            with self.assertRaises(WebSocketError):
                WebSocketClient(url)
