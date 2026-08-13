from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import AF_INET, AF_INET6, IPPROTO_TCP, SOCK_STREAM
from ssl import SSLContext
from threading import Thread
from time import monotonic
from typing import Any, cast
from unittest import TestCase
from unittest.mock import Mock, call, patch

from util.http import (
    DEFAULT_MAX_BYTES,
    HTTPClient,
    InvalidResponseError,
    Response,
    ResponseTooLargeError,
    ResolvedAddress,
    UnsafeAddressError,
    _PinnedHTTPSConnection,
    _ValidatedAddressConnector,
    _validated_addresses,
    http,
)


class FakeRawResponse:
    def __init__(self, status: int, body: bytes = b"", headers: tuple = ()) -> None:
        self.status = status
        self.reason = "OK"
        self.body = body
        self.headers = headers
        self.offset = 0

    def getheaders(self) -> tuple[Any, ...]:
        return self.headers

    def read(self, amount: int) -> bytes:
        data = self.body[self.offset : self.offset + amount]
        self.offset += len(data)
        return data


class FakeConnection:
    def __init__(self, response: FakeRawResponse) -> None:
        self.response = response
        self.requested: Any = None

    def request(
        self, method: Any, path: Any, body: Any = None, headers: Any = None
    ) -> None:
        self.requested = (method, path, body, headers)

    def getresponse(self) -> FakeRawResponse:
        return self.response

    def close(self) -> None:
        pass


def resolved(ip: str, port: int = 443) -> tuple[ResolvedAddress, ...]:
    if ":" in ip:
        return (ResolvedAddress(AF_INET6, SOCK_STREAM, IPPROTO_TCP, (ip, port, 0, 0)),)
    return (ResolvedAddress(AF_INET, SOCK_STREAM, IPPROTO_TCP, (ip, port)),)


class HostRecordingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.received_host = self.headers["Host"]  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:
        pass


class HTTPClientTest(TestCase):
    def test_invalid_json_is_reported_as_an_http_response_error(self) -> None:
        response = Response(
            url="https://example.test/data",
            status=200,
            reason="OK",
            headers={},
            body=b"not JSON",
        )
        with self.assertRaisesRegex(InvalidResponseError, "example.test/data"):
            response.json()

    def test_rejects_private_or_mixed_dns_answers(self) -> None:
        answers = [
            (AF_INET, SOCK_STREAM, IPPROTO_TCP, "", ("1.1.1.1", 80)),
            (AF_INET, SOCK_STREAM, IPPROTO_TCP, "", ("127.0.0.1", 80)),
        ]
        with patch("util.http.getaddrinfo", return_value=answers):
            with self.assertRaises(UnsafeAddressError):
                _validated_addresses("example.test", 80, False)

    def test_retains_all_validated_public_dns_answers(self) -> None:
        answers = [
            (
                AF_INET6,
                SOCK_STREAM,
                IPPROTO_TCP,
                "",
                ("2606:4700:4700::1111", 443, 0, 0),
            ),
            (AF_INET, SOCK_STREAM, IPPROTO_TCP, "", ("1.1.1.1", 443)),
        ]
        with patch("util.http.getaddrinfo", return_value=answers):
            addresses = _validated_addresses("example.test", 443, False)

        self.assertEqual(
            [address.ip for address in addresses], [answers[0][4][0], "1.1.1.1"]
        )

    def test_connection_fails_over_across_exact_validated_ipv6_and_ipv4(self) -> None:
        ipv6 = resolved("2606:4700:4700::1111")[0]
        ipv4 = resolved("1.1.1.1")[0]
        failed_socket = Mock()
        failed_socket.connect.side_effect = OSError("unreachable")
        connected_socket = Mock()

        with patch(
            "util.http.socket", side_effect=[failed_socket, connected_socket]
        ) as socket_factory:
            connected = _ValidatedAddressConnector(
                (ipv6, ipv4), monotonic() + 1
            ).connect()

        self.assertIs(connected, connected_socket)
        self.assertEqual(
            socket_factory.call_args_list,
            [
                call(AF_INET6, SOCK_STREAM, IPPROTO_TCP),
                call(AF_INET, SOCK_STREAM, IPPROTO_TCP),
            ],
        )
        failed_socket.connect.assert_called_once_with(ipv6.sockaddr)
        connected_socket.connect.assert_called_once_with(ipv4.sockaddr)
        failed_socket.close.assert_called_once_with()

    def test_pinned_https_connection_uses_original_hostname_for_tls(self) -> None:
        raw_socket = Mock()
        tls_socket = Mock()
        context = Mock(spec=SSLContext)
        context.wrap_socket.return_value = tls_socket
        connector = Mock(spec=_ValidatedAddressConnector)
        connector.connect.side_effect = lambda prepare: prepare(raw_socket)
        connection = _PinnedHTTPSConnection(
            "service.example", 443, connector, 1.0, cast(SSLContext, context)
        )

        connection.connect()

        self.assertIs(connection.sock, tls_socket)
        context.wrap_socket.assert_called_once_with(
            raw_socket, server_hostname="service.example"
        )

    def test_pinned_connection_retains_original_http_host_header(self) -> None:
        try:
            server = HTTPServer(("127.0.0.1", 0), HostRecordingHandler)
        except PermissionError:
            self.skipTest("localhost sockets are unavailable")
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]
        answers = [
            (AF_INET, SOCK_STREAM, IPPROTO_TCP, "", ("127.0.0.1", port)),
        ]
        try:
            with patch("util.http.getaddrinfo", return_value=answers):
                response = HTTPClient(allow_private=True).get(
                    "http://original.example:%d/resource" % port
                )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)

        self.assertEqual(response.body, b"ok")
        self.assertEqual(cast(Any, server).received_host, "original.example:%d" % port)

    def test_revalidates_each_redirect_hostname(self) -> None:
        connections = [
            FakeConnection(
                FakeRawResponse(302, headers=(("Location", "https://two.test/x"),))
            ),
            FakeConnection(FakeRawResponse(200, b"done")),
        ]
        with (
            patch(
                "util.http._validated_addresses",
                side_effect=[resolved("1.1.1.1"), resolved("8.8.8.8")],
            ) as validate,
            patch("util.http._connection", side_effect=connections),
        ):
            response = HTTPClient().get("https://one.test/start")
        self.assertEqual(response.body, b"done")
        self.assertEqual(
            [call.args[0] for call in validate.call_args_list], ["one.test", "two.test"]
        )

    def test_enforces_streamed_response_limit(self) -> None:
        connection = FakeConnection(FakeRawResponse(200, b"12345"))
        with (
            patch("util.http._validated_addresses", return_value=resolved("1.1.1.1")),
            patch("util.http._connection", return_value=connection),
        ):
            with self.assertRaises(ResponseTooLargeError):
                HTTPClient(max_bytes=4).get("https://example.test/")

    def test_shared_client_enforces_the_two_megabyte_limit(self) -> None:
        connection = FakeConnection(
            FakeRawResponse(
                200,
                headers=(("Content-Length", str(DEFAULT_MAX_BYTES + 1)),),
            )
        )
        with (
            patch("util.http._validated_addresses", return_value=resolved("1.1.1.1")),
            patch("util.http._connection", return_value=connection),
        ):
            with self.assertRaises(ResponseTooLargeError):
                http.get("https://example.test/resource")

        self.assertEqual(http.max_bytes, DEFAULT_MAX_BYTES)
