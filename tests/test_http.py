from socket import AF_INET, SOCK_STREAM
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from util.http import (
    HTTPClient,
    InvalidResponseError,
    Response,
    ResponseTooLargeError,
    UnsafeAddressError,
    _validated_address,
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
            (AF_INET, SOCK_STREAM, 6, "", ("203.0.113.1", 80)),
            (AF_INET, SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
        ]
        with patch("util.http.getaddrinfo", return_value=answers):
            with self.assertRaises(UnsafeAddressError):
                _validated_address("example.test", 80, False)

    def test_revalidates_each_redirect_hostname(self) -> None:
        connections = [
            FakeConnection(
                FakeRawResponse(302, headers=(("Location", "https://two.test/x"),))
            ),
            FakeConnection(FakeRawResponse(200, b"done")),
        ]
        with (
            patch(
                "util.http._validated_address", side_effect=["1.1.1.1", "2.2.2.2"]
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
            patch("util.http._validated_address", return_value="1.1.1.1"),
            patch("util.http._connection", return_value=connection),
        ):
            with self.assertRaises(ResponseTooLargeError):
                HTTPClient(max_bytes=4).get("https://example.test/")
