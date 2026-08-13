from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from ipaddress import ip_address
from json import JSONDecodeError, loads
from socket import SOCK_STREAM, create_connection, getaddrinfo
from ssl import create_default_context
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit


DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5


class HTTPError(RuntimeError):
    pass


class UnsafeAddressError(HTTPError):
    pass


class ResponseTooLargeError(HTTPError):
    pass


class HTTPTimeoutError(HTTPError):
    pass


class InvalidResponseError(HTTPError):
    pass


class HTTPStatusError(HTTPError):
    def __init__(self, response: "Response") -> None:
        super().__init__(
            "HTTP %d %s for %s" % (response.status, response.reason, response.url)
        )
        self.response = response


@dataclass(frozen=True, slots=True)
class Response:
    url: str
    status: int
    reason: str
    headers: Mapping[str, str]
    body: bytes

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        for parameter in content_type.split(";")[1:]:
            name, separator, value = parameter.strip().partition("=")
            if separator and name.casefold() == "charset":
                charset = value.strip("\"'")
                break
        try:
            return self.body.decode(charset, "replace")
        except LookupError:
            return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return loads(self.body)
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidResponseError(
                "Invalid JSON response from %s" % self.url
            ) from exc


def _validated_address(hostname: str, port: int, allow_private: bool) -> str:
    try:
        addresses = {
            str(entry[4][0]) for entry in getaddrinfo(hostname, port, type=SOCK_STREAM)
        }
    except OSError as exc:
        raise HTTPError("Could not resolve %s: %s" % (hostname, exc)) from exc
    if not addresses:
        raise HTTPError("No address found for %s" % hostname)

    unsafe = sorted(
        address
        for address in addresses
        if not ip_address(address.split("%", 1)[0]).is_global
    )
    if unsafe and not allow_private:
        raise UnsafeAddressError(
            "Refusing non-public address for %s: %s" % (hostname, ", ".join(unsafe))
        )
    return sorted(addresses)[0]


def _connection(
    scheme: str,
    hostname: str,
    port: int,
    address: str,
    timeout: float,
) -> HTTPConnection:
    if scheme == "https":
        connection: HTTPConnection = HTTPSConnection(
            hostname, port, timeout=timeout, context=create_default_context()
        )
    else:
        connection = HTTPConnection(hostname, port, timeout=timeout)

    # Pin the actual socket to the address we validated while retaining the
    # original hostname for the Host header and HTTPS SNI/certificate checks.
    connection._create_connection = (  # type: ignore[attr-defined,method-assign]
        lambda _target, socket_timeout, source_address=None: create_connection(
            (address, port), socket_timeout, source_address
        )
    )
    return connection


def _read_limited(
    response: HTTPResponse,
    max_bytes: int,
    deadline: float,
    configured_timeout: float,
) -> bytes:
    """Read incrementally so both the byte limit and wall-clock deadline apply."""
    chunks: list[bytes] = []
    size = 0
    read = getattr(response, "read1", response.read)
    while size <= max_bytes:
        if monotonic() >= deadline:
            raise HTTPTimeoutError(
                "HTTP request exceeded %.1f seconds" % configured_timeout
            )
        chunk = read(min(64 * 1024, max_bytes + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if monotonic() >= deadline:
            raise HTTPTimeoutError(
                "HTTP request exceeded %.1f seconds" % configured_timeout
            )
    body = b"".join(chunks)
    if len(body) > max_bytes:
        raise ResponseTooLargeError("Response exceeds %d bytes" % max_bytes)
    return body


class HTTPClient:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        allow_private: bool = False,
        user_agent: str = "pyBurlyBot/3",
    ) -> None:
        if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
            raise ValueError("HTTP limits must be positive.")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.allow_private = allow_private
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        raise_for_status: bool = True,
    ) -> Response:
        method = method.upper()
        request_headers = {
            "Accept-Encoding": "identity",
            "User-Agent": self.user_agent,
            **(dict(headers) if headers else {}),
        }
        if any(name.casefold() == "host" for name in request_headers):
            raise ValueError("The Host header is managed by HTTPClient.")

        current_url = url
        deadline = monotonic() + self.timeout
        for redirect_count in range(self.max_redirects + 1):
            parsed = urlsplit(current_url)
            scheme = parsed.scheme.casefold()
            if scheme not in {"http", "https"}:
                raise UnsafeAddressError("Only HTTP and HTTPS URLs are allowed.")
            if parsed.username is not None or parsed.password is not None:
                raise UnsafeAddressError("Credentials in URLs are not allowed.")
            if not parsed.hostname:
                raise HTTPError("URL has no hostname: %s" % current_url)
            try:
                hostname = parsed.hostname.encode("idna").decode("ascii")
                port = parsed.port or (443 if scheme == "https" else 80)
            except (UnicodeError, ValueError) as exc:
                raise HTTPError("Invalid URL authority: %s" % current_url) from exc

            remaining = deadline - monotonic()
            if remaining <= 0:
                raise HTTPTimeoutError(
                    "HTTP request exceeded %.1f seconds" % self.timeout
                )
            address = _validated_address(hostname, port, self.allow_private)
            connection = _connection(scheme, hostname, port, address, remaining)
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            try:
                connection.request(method, path, body=body, headers=request_headers)
                raw_response = connection.getresponse()
                response_headers = {
                    name.casefold(): value for name, value in raw_response.getheaders()
                }
                content_encoding = response_headers.get(
                    "content-encoding", "identity"
                ).casefold()
                if content_encoding not in {"", "identity"}:
                    raise HTTPError(
                        "Unsupported Content-Encoding: %s" % content_encoding
                    )
                declared_size = response_headers.get("content-length")
                if declared_size is not None:
                    try:
                        parsed_size = int(declared_size)
                    except ValueError as exc:
                        raise HTTPError(
                            "Invalid Content-Length response header"
                        ) from exc
                    if parsed_size < 0:
                        raise HTTPError(
                            "Invalid negative Content-Length response header"
                        )
                    if parsed_size > self.max_bytes:
                        raise ResponseTooLargeError(
                            "Response exceeds %d bytes" % self.max_bytes
                        )
                response_body = (
                    b""
                    if method == "HEAD"
                    else _read_limited(
                        raw_response, self.max_bytes, deadline, self.timeout
                    )
                )
                response = Response(
                    url=current_url,
                    status=raw_response.status,
                    reason=raw_response.reason,
                    headers=response_headers,
                    body=response_body,
                )
            except HTTPTimeoutError:
                raise
            except TimeoutError as exc:
                raise HTTPTimeoutError(
                    "HTTP request exceeded %.1f seconds" % self.timeout
                ) from exc
            except OSError as exc:
                raise HTTPError(
                    "HTTP request failed for %s: %s" % (current_url, exc)
                ) from exc
            finally:
                connection.close()

            if response.status not in {301, 302, 303, 307, 308}:
                if raise_for_status and not 200 <= response.status < 300:
                    raise HTTPStatusError(response)
                return response

            location = response.headers.get("location")
            if not location:
                if raise_for_status:
                    raise HTTPStatusError(response)
                return response
            if redirect_count == self.max_redirects:
                raise HTTPError("Too many redirects for %s" % url)
            next_url = urljoin(current_url, location)
            old_origin = (scheme, hostname, port)
            next_parsed = urlsplit(next_url)
            next_origin = (
                next_parsed.scheme.casefold(),
                (next_parsed.hostname or "").casefold(),
                next_parsed.port
                or (443 if next_parsed.scheme.casefold() == "https" else 80),
            )
            if next_origin != old_origin:
                request_headers = {
                    key: value
                    for key, value in request_headers.items()
                    if key.casefold() not in {"authorization", "cookie"}
                }
            if response.status == 303 or (
                response.status in {301, 302} and method == "POST"
            ):
                method, body = "GET", None
                request_headers.pop("Content-Type", None)
                request_headers.pop("Content-Length", None)
            current_url = next_url

        raise AssertionError("redirect loop terminated unexpectedly")

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.get(url, **kwargs).text

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        request_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if headers:
            request_headers.update(headers)
        return self.request(
            "POST",
            url,
            headers=request_headers,
            body=urlencode(fields).encode("ascii"),
            **kwargs,
        )


http = HTTPClient()
