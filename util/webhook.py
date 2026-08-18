"""HTTP listener plumbing for the ``webhook`` module.

The listening socket is owned here, by core, rather than by the module: on a
module hot-reload ``unload()`` and ``init()`` run in the same reactor tick and
a port that is still being closed cannot be re-bound until the next iteration.
Keeping the port here lets a reload simply swap the handler and only re-bind
when the configured address actually changed.

Policy (authentication, which events are posted) lives in the module; this file
only turns HTTP requests into :class:`WebhookRequest` values and writes the
handler's JSON reply back.

All ``WebhookListener`` methods must be called from the reactor thread (module
``init()``/``unload()`` already are).
"""

from collections.abc import Callable
from dataclasses import dataclass
from json import dumps
from logging import getLogger
from typing import Any, ClassVar

from twisted.internet import reactor as _reactor
from twisted.internet.defer import Deferred, maybeDeferred, succeed
from twisted.python import log
from twisted.python.failure import Failure
from twisted.web.resource import Resource
from twisted.web.server import Request, Site

reactor: Any = _reactor

DEFAULT_MAX_BODY = 64 * 1024
# idle keep-alive connections are dropped after this many seconds
CONNECTION_TIMEOUT = 30


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    """A received HTTP request, decoded for module consumption."""

    method: str
    path: str
    # header names lower-cased; only the first value of repeated headers
    headers: dict[str, str]
    # query string (and form) arguments
    args: dict[str, list[str]]
    body: bytes
    remote: str

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


# returns (HTTP status code, JSON-serialisable payload or plain text)
WebhookHandler = Callable[[WebhookRequest], tuple[int, Any]]


class _LimitedRequest(Request):
    """Reject oversized bodies as early as their length is announced."""

    def gotLength(self, length: int | None) -> None:
        limit = WebhookListener.max_body
        if length is not None and length > limit:
            transport: Any = self.channel.transport
            transport.write(
                b"HTTP/1.1 413 Payload Too Large\r\n"
                b"Connection: close\r\nContent-Length: 0\r\n\r\n"
            )
            transport.loseConnection()
            return
        Request.gotLength(self, length)


class _WebhookRoot(Resource):
    isLeaf = True

    def render(self, request: Request) -> bytes:
        request.setHeader(b"server", b"pyBurlyBot")
        request.setHeader(b"cache-control", b"no-store")
        handler = WebhookListener.handler
        if handler is None:
            return _reply(request, 503, {"error": "webhook handler not loaded"})

        limit = WebhookListener.max_body
        body = request.content.read(limit + 1) if request.content is not None else b""
        if len(body) > limit:
            return _reply(request, 413, {"error": "payload too large"})

        try:
            status, payload = handler(_decode_request(request, body))
        except Exception:  # noqa: BLE001 - module handler boundary
            log.err(None, "WEBHOOK: handler failed")
            status, payload = 500, {"error": "internal error"}
        return _reply(request, status, payload)


def _decode_request(request: Request, body: bytes) -> WebhookRequest:
    headers: dict[str, str] = {}
    for name, values in request.requestHeaders.getAllRawHeaders():
        if values:
            headers[_text(name).lower()] = _text(values[0])
    args = {
        _text(name): [_text(value) for value in values]
        for name, values in (request.args or {}).items()
    }
    address = request.getClientAddress()
    return WebhookRequest(
        method=_text(request.method).upper(),
        path=_text(request.path),
        headers=headers,
        args=args,
        body=body,
        remote=str(getattr(address, "host", "") or ""),
    )


def _text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _reply(request: Request, status: int, payload: Any) -> bytes:
    request.setResponseCode(status)
    if isinstance(payload, str):
        request.setHeader(b"content-type", b"text/plain; charset=utf-8")
        return payload.encode("utf-8")
    request.setHeader(b"content-type", b"application/json; charset=utf-8")
    return dumps(payload).encode("utf-8") + b"\n"


class WebhookListener:
    """Process-wide owner of the webhook listening port.

    ``listen()`` is idempotent for an unchanged address (it just installs the
    new handler), which is what a module hot-reload needs. ``release()`` drops
    the handler and closes the port on the next reactor iteration unless a
    handler was installed again in the meantime (i.e. the module was reloaded
    rather than removed).
    """

    handler: ClassVar[WebhookHandler | None] = None
    max_body: ClassVar[int] = DEFAULT_MAX_BODY
    _port: ClassVar[Any] = None
    _address: ClassVar[tuple[str, int] | None] = None
    # serialises stop/start so a rebind never races an in-flight close
    _chain: ClassVar[Deferred[Any]] = succeed(None)

    @classmethod
    def listen(
        cls,
        host: str,
        port: int,
        handler: WebhookHandler,
        max_body: int = DEFAULT_MAX_BODY,
    ) -> None:
        cls.handler = handler
        cls.max_body = int(max_body)
        address = (host, int(port))
        if cls._address == address:
            return
        cls._address = address
        cls._enqueue(cls._stop_port)
        cls._enqueue(lambda: cls._start_port(address))

    @classmethod
    def release(cls) -> None:
        cls.handler = None
        reactor.callLater(0, cls._stop_if_unclaimed)

    @classmethod
    def address(cls) -> tuple[str, int] | None:
        """The address the listener is bound (or binding) to, if any."""
        return cls._address

    @classmethod
    def _stop_if_unclaimed(cls) -> None:
        if cls.handler is None:
            cls._address = None
            cls._enqueue(cls._stop_port)

    @classmethod
    def _enqueue(cls, step: Callable[[], Any]) -> None:
        def run(_: Any) -> Any:
            return maybeDeferred(step)

        cls._chain.addCallback(run)
        cls._chain.addErrback(cls._log_failure)

    @classmethod
    def _log_failure(cls, failure: Failure) -> None:
        log.err(failure, "WEBHOOK: listener error")

    @classmethod
    def _stop_port(cls) -> Any:
        port, cls._port = cls._port, None
        if port is None:
            return None
        return maybeDeferred(port.stopListening)

    @classmethod
    def _start_port(cls, address: tuple[str, int]) -> None:
        if cls._address != address:
            # superseded by a later listen()/release() before we got here
            return
        host, port = address
        try:
            cls._port = reactor.listenTCP(
                port,
                Site(
                    _WebhookRoot(),
                    requestFactory=_LimitedRequest,
                    timeout=CONNECTION_TIMEOUT,
                ),
                interface=host,
            )
        except Exception:
            cls._address = None
            raise
        bound = getattr(cls._port.getHost(), "port", port)
        getLogger(__name__).info("WEBHOOK: listening on http://%s:%d/", host, bound)
