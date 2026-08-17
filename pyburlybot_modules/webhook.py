from util.event import Event
from util.types import BotLike
# webhook module: exposes a small HTTP endpoint and turns requests into bot events.
#
#   POST http://<listen_host>:<listen_port>/hooks/<name>      (prefix: path_prefix option)
#
# Every request posts a "webhook" event (event.hook == <name>) to all servers.
# Names listed in the event_hooks option are additionally posted as their own
# event type, e.g. /hooks/reload -> a "reload" event and /hooks/update -> an
# "update" event, so modules can listen for a purpose-named event without
# knowing it came from HTTP.
#
# Requests that present the configured secret are marked event.authorized=True:
#   Authorization: Bearer <secret>
#   X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body>   (GitHub style)
# Anything else is still delivered, with event.authorized=False. Handlers that
# do privileged things must check event.authorized themselves.
#
# Event attributes: hook, method, path, headers, args, body, json, remote,
# authorized, event_id. Handlers get the plain Container (no reply target).
#
# All options are global (server=False): there is one listener per bot process.

from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from json import JSONDecodeError, loads
from re import compile as recompile
from typing import Any
from uuid import uuid4

from util import Mapping, Option
from util.webhook import DEFAULT_MAX_BODY, WebhookListener, WebhookRequest

DEFAULT_PATH_PREFIX = "/hooks/"

OPTIONS = {
    "listen_host": (
        str,
        "Address the webhook listener binds to. Inside a container use 0.0.0.0 "
        "and publish the port to localhost only. Takes effect on reload.",
        "127.0.0.1",
    ),
    "listen_port": (
        int,
        "TCP port of the webhook listener. 0 disables it. Takes effect on reload.",
        8642,
    ),
    "secret": Option(
        str,
        "Shared secret that marks a request as authorized (event.authorized). Send it "
        "as 'Authorization: Bearer <secret>' or sign the body with HMAC-SHA256 in "
        "'X-Hub-Signature-256: sha256=<hex>'. Empty: no request is ever authorized.",
        "",
        secret=True,
        writeonly=True,
    ),
    "event_hooks": (
        list,
        "Hook names that are also posted as their own event type (e.g. 'reload', "
        "'update'). Every request posts a generic 'webhook' event regardless.",
        ["reload", "update"],
    ),
    "max_body": (
        int,
        "Largest accepted request body in bytes.",
        DEFAULT_MAX_BODY,
    ),
    "path_prefix": (
        str,
        "URL path under which hooks are served: <path_prefix><name>. "
        "Leading/trailing slashes are normalized. Applies immediately.",
        DEFAULT_PATH_PREFIX,
    ),
}
_HOOK_NAME = recompile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT"})


class _State:
    # setup container of the first server that loaded us; used to post (broadcast) events
    bot: BotLike | None = None


def _option(name: str) -> Any:
    if _State.bot is None:
        raise RuntimeError("webhook module is not initialized")
    return _State.bot.getOption(name, module="webhook", server=False)


def is_authorized(request: WebhookRequest, secret: str) -> bool:
    """True if ``request`` proves knowledge of ``secret``."""
    if not secret:
        return False
    expected = secret.encode("utf-8")

    scheme, _, token = request.header("authorization").partition(" ")
    if scheme.lower() == "bearer" and compare_digest(
        token.strip().encode("utf-8"), expected
    ):
        return True

    signature = request.header("x-hub-signature-256").strip()
    if signature:
        digest = hmac_new(expected, request.body, sha256).hexdigest()
        candidate = signature.partition("=")[2] if "=" in signature else signature
        if compare_digest(candidate.lower().encode("ascii"), digest.encode("ascii")):
            return True
    return False


def normalize_prefix(prefix: object) -> str:
    """Coerce a configured path prefix to the '/segment/.../' form."""
    text = str(prefix or "").strip().strip("/")
    return "/%s/" % text if text else "/"


def hook_name(path: str, prefix: str = DEFAULT_PATH_PREFIX) -> str | None:
    """Extract and validate the hook name from a request path."""
    if not path.startswith(prefix):
        return None
    name = path[len(prefix) :].strip("/")
    if not _HOOK_NAME.match(name):
        return None
    return name.lower()


def handle(request: WebhookRequest) -> tuple[int, Any]:
    """Turn one HTTP request into bot events. Runs in the reactor thread."""
    if request.path == "/health":
        return 200, {"ok": True}
    bot = _State.bot
    if bot is None:
        return 503, {"error": "webhook module not initialized"}
    hook = hook_name(request.path, normalize_prefix(_option("path_prefix")))
    if hook is None:
        return 404, {"error": "unknown hook"}
    if request.method not in _ALLOWED_METHODS:
        return 405, {"error": "method not allowed"}

    payload = None
    if request.body and "json" in request.header("content-type").lower():
        try:
            payload = loads(request.body)
        except JSONDecodeError, UnicodeDecodeError:
            return 400, {"error": "invalid JSON body"}

    authorized = is_authorized(request, str(_option("secret") or ""))
    promoted = hook != "webhook" and hook in {
        str(name).lower() for name in (_option("event_hooks") or ())
    }
    event_id = uuid4().hex
    attributes = {
        "hook": hook,
        "method": request.method,
        "path": request.path,
        "headers": request.headers,
        "args": request.args,
        "body": request.body.decode("utf-8", "replace"),
        "json": payload,
        "remote": request.remote,
        "authorized": authorized,
        "event_id": event_id,
    }
    print(
        "WEBHOOK: %s %s from %s authorized=%s%s"
        % (
            request.method,
            request.path,
            request.remote or "?",
            authorized,
            " (posting '%s' event)" % hook if promoted else "",
        )
    )
    bot.postEvent("webhook", broadcast=True, **attributes)
    if promoted:
        bot.postEvent(hook, broadcast=True, **attributes)
    return 202, {
        "ok": True,
        "hook": hook,
        "authorized": authorized,
        "event": hook if promoted else "webhook",
        "event_id": event_id,
    }


def webhook_status(event: Event, bot: BotLike) -> None:
    """webhook: show the listener address and whether a secret is configured."""
    address = WebhookListener.address()
    if address is None:
        listening = "not listening"
    else:
        prefix = normalize_prefix(
            bot.getOption("path_prefix", module="webhook", server=False)
        )
        listening = "listening on http://%s:%d%s<name>" % (*address, prefix)
    secret = bot.getOption("secret", module="webhook", server=False)
    hooks = bot.getOption("event_hooks", module="webhook", server=False) or []
    bot.say(
        "Webhook %s; secret %s; hooks posted as their own event: %s"
        % (
            listening,
            "configured" if secret else "NOT configured (all requests anonymous)",
            ", ".join(str(name) for name in hooks) or "none",
        )
    )


def init(bot: BotLike) -> bool:
    if _State.bot is None:
        _State.bot = bot
    port = int(bot.getOption("listen_port", module="webhook", server=False) or 0)
    if port <= 0:
        print("WEBHOOK: listener disabled (listen_port is 0)")
        WebhookListener.release()
        return True
    host = str(
        bot.getOption("listen_host", module="webhook", server=False) or "127.0.0.1"
    )
    max_body = int(
        bot.getOption("max_body", module="webhook", server=False) or DEFAULT_MAX_BODY
    )
    # idempotent across servers and across hot-reloads on an unchanged address
    WebhookListener.listen(host, port, handle, max_body=max_body)
    return True


def unload() -> None:
    _State.bot = None
    # closes the port unless init() claims it again in this same reactor tick (hot-reload)
    WebhookListener.release()


mappings = (Mapping(command="webhook", function=webhook_status, admin=True),)
