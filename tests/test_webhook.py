from hashlib import sha256
from hmac import new as hmac_new
from io import BytesIO
from json import loads
from typing import Any, cast
from unittest import TestCase
from unittest.mock import Mock, patch

from twisted.internet.address import IPv4Address
from twisted.internet.defer import Deferred, succeed
from twisted.web.http_headers import Headers

from pyburlybot_modules import reload as reload_module
from pyburlybot_modules import updaterelaunch
from pyburlybot_modules import webhook as webhook_module
from util.event import Event
from util.webhook import (
    DEFAULT_MAX_BODY,
    WebhookListener,
    WebhookRequest,
    _WebhookRoot,
)


def make_request(
    path: str = "/hooks/reload",
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    args: dict[str, list[str]] | None = None,
    remote: str = "10.0.0.5",
) -> WebhookRequest:
    return WebhookRequest(
        method=method,
        path=path,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        args=args or {},
        body=body,
        remote=remote,
    )


class FakeBot:
    """Stand-in for the bot Container the module keeps from init()."""

    network = "test-server"

    def __init__(self, **options: Any) -> None:
        self.options = {
            "listen_host": "127.0.0.1",
            "listen_port": 8642,
            "secrets": {},
            "event_hooks": ["reload"],
            "max_body": DEFAULT_MAX_BODY,
            "path_prefix": "/hooks/",
        }
        self.options.update(options)
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:  # satisfy the BotLike protocol
        raise AttributeError(name)

    def say(self, msg: Any, **kwargs: Any) -> None:
        raise AssertionError("posted-event handlers have no reply target")

    def getOption(self, opt: str, **kwargs: Any) -> Any:
        return self.options[opt]

    def setOption(self, opt: str, value: Any, **kwargs: Any) -> None:
        self.options[opt] = value

    def postEvent(self, event_type: str, **kwargs: Any) -> None:
        self.posted.append((event_type, kwargs))


class WebhookModuleTest(TestCase):
    def setUp(self) -> None:
        self.bot = FakeBot(secrets={"reload": "s3cret", "Update": "upd"})
        webhook_module._State.bot = self.bot
        self.addCleanup(setattr, webhook_module._State, "bot", None)

    def test_bearer_secret_authorizes(self) -> None:
        request = make_request(headers={"Authorization": "Bearer s3cret"})
        self.assertTrue(webhook_module.is_authorized(request, "s3cret"))
        self.assertFalse(webhook_module.is_authorized(request, "other"))
        self.assertFalse(
            webhook_module.is_authorized(
                make_request(headers={"Authorization": "Basic s3cret"}), "s3cret"
            )
        )

    def test_hmac_signature_authorizes(self) -> None:
        body = b'{"reason": "config changed"}'
        digest = hmac_new(b"s3cret", body, sha256).hexdigest()
        signed = make_request(
            headers={"X-Hub-Signature-256": "sha256=" + digest}, body=body
        )
        self.assertTrue(webhook_module.is_authorized(signed, "s3cret"))
        tampered = make_request(
            headers={"X-Hub-Signature-256": "sha256=" + digest}, body=body + b" "
        )
        self.assertFalse(webhook_module.is_authorized(tampered, "s3cret"))

    def test_no_configured_secret_never_authorizes(self) -> None:
        request = make_request(headers={"Authorization": "Bearer "})
        self.assertFalse(webhook_module.is_authorized(request, ""))

    def test_hook_secret_lookup_is_per_hook_and_case_insensitive(self) -> None:
        secrets = {"reload": "a", "Update": "b", "empty": ""}
        self.assertEqual(webhook_module.hook_secret(secrets, "reload"), "a")
        self.assertEqual(webhook_module.hook_secret(secrets, "update"), "b")
        self.assertEqual(webhook_module.hook_secret(secrets, "empty"), "")
        self.assertEqual(webhook_module.hook_secret(secrets, "other"), "")
        self.assertEqual(webhook_module.hook_secret(None, "reload"), "")
        self.assertEqual(webhook_module.hook_secret("s3cret", "reload"), "")

    def test_secret_authorizes_only_its_own_hook(self) -> None:
        bearer = {"Authorization": "Bearer s3cret"}  # the reload secret
        _, own = webhook_module.handle(make_request("/hooks/reload", headers=bearer))
        _, other = webhook_module.handle(make_request("/hooks/update", headers=bearer))
        _, none = webhook_module.handle(make_request("/hooks/github", headers=bearer))
        self.assertTrue(own["authorized"])
        self.assertFalse(other["authorized"])
        self.assertFalse(none["authorized"])
        # and the update secret works for update (config key case does not matter)
        _, upd = webhook_module.handle(
            make_request("/hooks/update", headers={"Authorization": "Bearer upd"})
        )
        self.assertTrue(upd["authorized"])
        self.assertEqual(
            [kw["authorized"] for _, kw in self.bot.posted],
            [True, True, False, False, True],  # reload posts 2 events (promoted)
        )

    def test_hook_names_are_validated_and_lowercased(self) -> None:
        self.assertEqual(webhook_module.hook_name("/hooks/Reload"), "reload")
        self.assertEqual(webhook_module.hook_name("/hooks/ci.build-1/"), "ci.build-1")
        self.assertIsNone(webhook_module.hook_name("/hooks/"))
        self.assertIsNone(webhook_module.hook_name("/hooks/a/b"))
        self.assertIsNone(webhook_module.hook_name("/other/reload"))
        self.assertIsNone(webhook_module.hook_name("/hooks/../etc"))
        self.assertEqual(webhook_module.hook_name("/api/v1/x", "/api/v1/"), "x")
        self.assertIsNone(webhook_module.hook_name("/hooks/x", "/api/v1/"))

    def test_path_prefix_is_normalized_and_configurable(self) -> None:
        for raw, expected in (
            ("hooks", "/hooks/"),
            ("/hooks", "/hooks/"),
            (" /api/v1/hooks/ ", "/api/v1/hooks/"),
            ("", "/"),
            (None, "/"),
        ):
            self.assertEqual(webhook_module.normalize_prefix(raw), expected)

        self.bot.options["path_prefix"] = "bot/hooks"
        self.assertEqual(webhook_module.handle(make_request(path="/hooks/x"))[0], 404)
        status, payload = webhook_module.handle(make_request(path="/bot/hooks/x"))
        self.assertEqual((status, payload["hook"]), (202, "x"))
        # bare "/" prefix serves hooks at the root, /health still wins
        self.bot.options["path_prefix"] = "/"
        self.assertEqual(webhook_module.handle(make_request(path="/reload"))[0], 202)
        self.assertEqual(webhook_module.handle(make_request(path="/health"))[0], 200)

    def test_authorized_promoted_hook_posts_generic_and_named_events(self) -> None:
        body = b'{"changed": ["BurlyBot.json"]}'
        status, payload = webhook_module.handle(
            make_request(
                headers={
                    "Authorization": "Bearer s3cret",
                    "Content-Type": "application/json",
                },
                body=body,
            )
        )

        self.assertEqual(status, 202)
        self.assertEqual(payload["hook"], "reload")
        self.assertTrue(payload["authorized"])
        self.assertEqual(payload["event"], "reload")
        self.assertEqual(
            [event_type for event_type, _ in self.bot.posted], ["webhook", "reload"]
        )
        for _, kwargs in self.bot.posted:
            self.assertTrue(kwargs["broadcast"])
            self.assertTrue(kwargs["authorized"])
            self.assertEqual(kwargs["hook"], "reload")
            self.assertEqual(kwargs["json"], {"changed": ["BurlyBot.json"]})
            self.assertEqual(kwargs["remote"], "10.0.0.5")
            self.assertEqual(kwargs["event_id"], payload["event_id"])

    def test_anonymous_request_is_delivered_but_not_authorized(self) -> None:
        status, payload = webhook_module.handle(make_request(path="/hooks/github"))

        self.assertEqual(status, 202)
        self.assertFalse(payload["authorized"])
        self.assertEqual(payload["event"], "webhook")
        self.assertEqual(len(self.bot.posted), 1)
        event_type, kwargs = self.bot.posted[0]
        self.assertEqual(event_type, "webhook")
        self.assertFalse(kwargs["authorized"])
        self.assertEqual(kwargs["hook"], "github")

    def test_generic_webhook_name_is_never_promoted_twice(self) -> None:
        self.bot.options["event_hooks"] = ["webhook"]
        webhook_module.handle(make_request(path="/hooks/webhook"))
        self.assertEqual([t for t, _ in self.bot.posted], ["webhook"])

    def test_error_responses(self) -> None:
        self.assertEqual(webhook_module.handle(make_request(path="/nope"))[0], 404)
        self.assertEqual(webhook_module.handle(make_request(method="DELETE"))[0], 405)
        self.assertEqual(
            webhook_module.handle(
                make_request(
                    headers={"Content-Type": "application/json"}, body=b"{oops"
                )
            )[0],
            400,
        )
        self.assertEqual(webhook_module.handle(make_request(path="/health"))[0], 200)
        self.assertEqual(self.bot.posted, [])

        webhook_module._State.bot = None
        self.assertEqual(webhook_module.handle(make_request())[0], 503)

    def test_init_and_unload_drive_the_shared_listener(self) -> None:
        with (
            patch.object(WebhookListener, "listen") as listen,
            patch.object(WebhookListener, "release") as release,
        ):
            self.assertTrue(webhook_module.init(self.bot))
            listen.assert_called_once_with(
                "127.0.0.1", 8642, webhook_module.handle, max_body=DEFAULT_MAX_BODY
            )
            webhook_module.unload()
            release.assert_called_once_with()
            self.assertIsNone(webhook_module._State.bot)

            listen.reset_mock()
            release.reset_mock()
            disabled = FakeBot(listen_port=0)
            self.assertTrue(webhook_module.init(disabled))
            listen.assert_not_called()
            release.assert_called_once_with()


class FakeTwistedRequest:
    """Just enough of twisted.web.server.Request for _WebhookRoot.render."""

    def __init__(
        self,
        method: bytes = b"POST",
        path: bytes = b"/hooks/x",
        body: bytes = b"",
        headers: dict[bytes, bytes] | None = None,
        args: dict[bytes, list[bytes]] | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.content = BytesIO(body)
        self.requestHeaders = Headers()
        for name, value in (headers or {}).items():
            self.requestHeaders.addRawHeader(name, value)
        self.args = args or {}
        self.responseHeaders = Headers()
        self.code = 200

    def getClientAddress(self) -> IPv4Address:
        return IPv4Address("TCP", "192.0.2.9", 4242)

    def setHeader(self, name: bytes, value: bytes) -> None:
        self.responseHeaders.setRawHeaders(name, [value])

    def setResponseCode(self, code: int) -> None:
        self.code = code


class WebhookRootTest(TestCase):
    def setUp(self) -> None:
        self.saved = (WebhookListener.handler, WebhookListener.max_body)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        WebhookListener.handler, WebhookListener.max_body = self.saved

    def test_request_is_decoded_and_json_reply_written(self) -> None:
        seen: list[WebhookRequest] = []

        def handler(request: WebhookRequest) -> tuple[int, Any]:
            seen.append(request)
            return 202, {"ok": True}

        WebhookListener.handler = handler
        WebhookListener.max_body = 1024
        request = FakeTwistedRequest(
            body=b"payload",
            headers={b"X-Thing": b"one", b"Content-Type": b"text/plain"},
            args={b"a": [b"1", b"2"]},
        )
        out = _WebhookRoot().render(cast(Any, request))

        self.assertEqual(request.code, 202)
        self.assertEqual(loads(out), {"ok": True})
        self.assertEqual(
            request.responseHeaders.getRawHeaders(b"content-type"),
            [b"application/json; charset=utf-8"],
        )
        (decoded,) = seen
        self.assertEqual(decoded.method, "POST")
        self.assertEqual(decoded.path, "/hooks/x")
        self.assertEqual(decoded.body, b"payload")
        self.assertEqual(decoded.header("x-thing"), "one")
        self.assertEqual(decoded.args, {"a": ["1", "2"]})
        self.assertEqual(decoded.remote, "192.0.2.9")

    def test_missing_handler_and_oversized_body(self) -> None:
        WebhookListener.handler = None
        request = FakeTwistedRequest()
        _WebhookRoot().render(cast(Any, request))
        self.assertEqual(request.code, 503)

        WebhookListener.handler = lambda request: (200, {})
        WebhookListener.max_body = 4
        request = FakeTwistedRequest(body=b"12345")
        _WebhookRoot().render(cast(Any, request))
        self.assertEqual(request.code, 413)

    def test_handler_exceptions_become_500(self) -> None:
        def broken(request: WebhookRequest) -> tuple[int, Any]:
            raise RuntimeError("boom")

        WebhookListener.handler = broken
        request = FakeTwistedRequest()
        with patch("util.webhook.log"):
            out = _WebhookRoot().render(cast(Any, request))
        self.assertEqual(request.code, 500)
        self.assertEqual(loads(out), {"error": "internal error"})


class FakePort:
    def __init__(self) -> None:
        self.stopped: Deferred[Any] | None = None

    def getHost(self) -> IPv4Address:
        return IPv4Address("TCP", "127.0.0.1", 8642)

    def stopListening(self) -> Deferred[Any]:
        self.stopped = Deferred()
        return self.stopped


class WebhookListenerTest(TestCase):
    def setUp(self) -> None:
        self.saved = (
            WebhookListener.handler,
            WebhookListener.max_body,
            WebhookListener._port,
            WebhookListener._address,
            WebhookListener._chain,
        )
        WebhookListener.handler = None
        WebhookListener._port = None
        WebhookListener._address = None
        WebhookListener._chain = succeed(None)
        self.reactor = Mock()
        self.reactor.listenTCP.side_effect = lambda *a, **kw: FakePort()
        patcher = patch("util.webhook.reactor", self.reactor)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        (
            WebhookListener.handler,
            WebhookListener.max_body,
            WebhookListener._port,
            WebhookListener._address,
            WebhookListener._chain,
        ) = self.saved

    def test_listen_binds_once_and_hot_reload_only_swaps_handler(self) -> None:
        first, second = Mock(name="first"), Mock(name="second")
        with patch("builtins.print"):
            WebhookListener.listen("127.0.0.1", 8642, first, max_body=10)
        port = WebhookListener._port
        self.reactor.listenTCP.assert_called_once()
        self.assertEqual(self.reactor.listenTCP.call_args.args[0], 8642)
        self.assertEqual(
            self.reactor.listenTCP.call_args.kwargs, {"interface": "127.0.0.1"}
        )
        self.assertIs(WebhookListener.handler, first)
        self.assertEqual(WebhookListener.max_body, 10)

        # simulate a hot-reload: unload() releases, init() re-listens in the same tick
        WebhookListener.release()
        self.assertIsNone(WebhookListener.handler)
        WebhookListener.listen("127.0.0.1", 8642, second)
        # the deferred close scheduled by release() finds the port claimed again
        callback = self.reactor.callLater.call_args.args[1]
        callback()

        self.assertIs(WebhookListener._port, port)
        self.assertIsNone(port.stopped)
        self.assertIs(WebhookListener.handler, second)
        self.reactor.listenTCP.assert_called_once()

    def test_release_without_reclaim_closes_the_port(self) -> None:
        with patch("builtins.print"):
            WebhookListener.listen("127.0.0.1", 8642, Mock())
        port = WebhookListener._port
        WebhookListener.release()
        self.reactor.callLater.call_args.args[1]()

        self.assertIsNotNone(port.stopped)
        self.assertIsNone(WebhookListener._port)
        self.assertIsNone(WebhookListener.address())

    def test_address_change_rebinds_after_the_old_port_closed(self) -> None:
        with patch("builtins.print"):
            WebhookListener.listen("127.0.0.1", 8642, Mock())
            old = WebhookListener._port
            WebhookListener.listen("0.0.0.0", 9000, Mock())  # noqa: S104 - fake reactor
            # old port is closing asynchronously; new bind waits for it
            self.assertIsNotNone(old.stopped)
            self.reactor.listenTCP.assert_called_once()
            old.stopped.callback(None)

        self.assertEqual(self.reactor.listenTCP.call_count, 2)
        self.assertEqual(self.reactor.listenTCP.call_args.args[0], 9000)
        self.assertEqual(WebhookListener.address(), ("0.0.0.0", 9000))  # noqa: S104

    def test_bind_failure_is_logged_and_forgotten(self) -> None:
        self.reactor.listenTCP.side_effect = OSError("address in use")
        with patch("util.webhook.log") as log:
            WebhookListener.listen("127.0.0.1", 8642, Mock())
        log.err.assert_called_once()
        self.assertIsNone(WebhookListener.address())
        self.assertIsNone(WebhookListener._port)


class ReloadEventTest(TestCase):
    def setUp(self) -> None:
        reload_module._seen_events.clear()

    def test_unauthorized_reload_event_is_ignored(self) -> None:
        with (
            patch.object(reload_module, "call_in_reactor") as call,
            patch("builtins.print"),
        ):
            reload_module.reload_event(Event("reload", remote="10.0.0.5"), Mock())
        call.assert_not_called()

    def test_authorized_reload_event_reloads_once_per_broadcast(self) -> None:
        with (
            patch.object(reload_module, "call_in_reactor") as call,
            patch("builtins.print"),
        ):
            for _ in range(2):  # same event delivered to two servers
                reload_module.reload_event(
                    Event("reload", authorized=True, event_id="abc"), Mock()
                )
            reload_module.reload_event(
                Event("reload", authorized=True, event_id="def"), Mock()
            )
        self.assertEqual(call.call_count, 2)
        for args in call.call_args_list:
            self.assertIs(args.args[0], reload_module._reallyReload)


class UpdateEventTest(TestCase):
    """updaterelaunch's handling of posted "update" events (webhook / GitHub push)."""

    def setUp(self) -> None:
        updaterelaunch._Pending.call = None
        updaterelaunch._Pending.bot = None
        updaterelaunch._seen_events.clear()
        updaterelaunch._restart_pending.clear()
        self.addCleanup(updaterelaunch._restart_pending.clear)
        self.bot = Mock()
        self.options = {
            "git_branch": "main",
            "update_debounce": 30,
            "auto_restart": True,
            "git_path": "git",
            "admins": ["clam"],
        }
        self.bot.getOption.side_effect = lambda name, **kw: self.options[name]
        self.reactor = Mock()
        patcher = patch.object(updaterelaunch, "reactor", self.reactor)
        patcher.start()
        self.addCleanup(patcher.stop)
        # run reactor.callFromThread(f, *a) synchronously
        self.reactor.callFromThread.side_effect = lambda f, *a, **kw: f(*a, **kw)

    def _event(self, **kwargs: Any) -> Event:
        kwargs.setdefault("authorized", True)
        kwargs.setdefault("remote", "140.82.112.1")
        return Event("update", **kwargs)

    def test_module_and_test_changes_do_not_require_a_restart(self) -> None:
        changes = "\n".join(
            (
                "M\tpyburlybot_modules/tell.py",
                "M\ttests/test_tell.py",
            )
        )
        self.assertEqual(
            updaterelaunch._classify_changes(changes),
            {"core": False, "modules": True, "deps": False, "any": True},
        )

    def test_renamed_runtime_files_classify_both_paths(self) -> None:
        changes = "R100\tutil/old.py\ttests/test_old.py"
        self.assertEqual(
            updaterelaunch._classify_changes(changes),
            {"core": True, "modules": False, "deps": False, "any": True},
        )

    def test_unauthorized_update_is_ignored(self) -> None:
        with patch("builtins.print"):
            updaterelaunch.update_event(self._event(authorized=False), self.bot)
        self.reactor.callLater.assert_not_called()

    def test_plain_authorized_update_schedules_a_debounced_check(self) -> None:
        with patch("builtins.print"):
            updaterelaunch.update_event(self._event(), self.bot)
        self.reactor.callLater.assert_called_once_with(30, updaterelaunch._fire_pending)
        self.assertIs(updaterelaunch._Pending.bot, self.bot)

    def test_burst_of_updates_resets_the_single_pending_check(self) -> None:
        pending = Mock()
        pending.active.return_value = True
        self.reactor.callLater.return_value = pending
        with patch("builtins.print"):
            for _ in range(3):
                updaterelaunch.update_event(self._event(), self.bot)
        self.reactor.callLater.assert_called_once()
        self.assertEqual(pending.reset.call_count, 2)
        pending.reset.assert_called_with(30)

    def test_debounce_zero_checks_immediately(self) -> None:
        self.bot.getOption.side_effect = lambda name, **kw: {
            "git_branch": "main",
            "update_debounce": 0,
        }[name]
        with patch("builtins.print"):
            updaterelaunch.update_event(self._event(), self.bot)
        self.reactor.callLater.assert_called_once_with(0, updaterelaunch._fire_pending)

    def test_fire_runs_the_check_in_a_worker_thread_once(self) -> None:
        updaterelaunch._Pending.bot = self.bot
        updaterelaunch._Pending.call = Mock()
        updaterelaunch._fire_pending()
        self.reactor.callInThread.assert_called_once_with(
            updaterelaunch._event_update_check, self.bot
        )
        self.assertIsNone(updaterelaunch._Pending.call)
        self.assertIsNone(updaterelaunch._Pending.bot)

    def test_github_deliveries_are_filtered_to_pushes_on_the_tracked_branch(
        self,
    ) -> None:
        github = {"x-github-event": "push"}
        cases: list[tuple[dict[str, Any], bool]] = [
            ({"headers": {"x-github-event": "ping"}, "json": {"zen": "..."}}, False),
            ({"headers": github, "json": {"ref": "refs/heads/feature"}}, False),
            ({"headers": github, "json": {"ref": "refs/tags/v1"}}, False),
            (
                {
                    "headers": github,
                    "json": {"ref": "refs/heads/main", "deleted": True},
                },
                False,
            ),
            ({"headers": github, "json": None}, False),
            ({"headers": github, "json": {"ref": "refs/heads/main"}}, True),
            # non-GitHub callers (e.g. curl from the orchestrator) are not filtered
            ({"headers": {"user-agent": "curl"}, "json": None}, True),
        ]
        for attributes, expected in cases:
            with self.subTest(attributes=attributes), patch("builtins.print"):
                self.assertIs(
                    updaterelaunch._github_push_for_branch(
                        self._event(**attributes), "main"
                    ),
                    expected,
                )
            self.reactor.callLater.reset_mock()
            updaterelaunch._Pending.call = None
            with patch("builtins.print"):
                updaterelaunch.update_event(self._event(**attributes), self.bot)
            self.assertEqual(self.reactor.callLater.called, expected)

    def test_event_check_hot_reloads_modules_and_restarts_for_core(self) -> None:
        with (
            patch.object(
                updaterelaunch,
                "_check_and_apply",
                return_value={
                    "core": False,
                    "modules": True,
                    "deps": False,
                    "any": True,
                },
            ),
            patch.object(updaterelaunch, "call_in_reactor") as call,
            patch("builtins.print"),
        ):
            updaterelaunch._event_update_check(self.bot)
        call.assert_called_once_with(updaterelaunch._reload_all)

        with (
            patch.object(
                updaterelaunch,
                "_check_and_apply",
                return_value={
                    "core": True,
                    "modules": False,
                    "deps": False,
                    "any": True,
                },
            ),
            patch.object(updaterelaunch, "_restart") as restart,
            patch("builtins.print"),
        ):
            updaterelaunch._event_update_check(self.bot)
        restart.assert_called_once_with()

    def test_event_check_failure_is_logged_and_reported_to_admins(self) -> None:
        with (
            patch.object(
                updaterelaunch, "_check_and_apply", side_effect=OSError("no git")
            ),
            patch("builtins.print") as printed,
        ):
            updaterelaunch._event_update_check(self.bot)
        self.assertIn("update check failed", printed.call_args.args[0])
        self.assertFalse(updaterelaunch._update_lock.locked())
        self.bot.sendmsg.assert_called_once_with(
            "clam", "Unattended update failed: no git"
        )

    def test_broadcast_copies_schedule_a_single_check_per_event_id(self) -> None:
        with patch("builtins.print"):
            for _ in range(3):  # same post delivered once per server
                updaterelaunch.update_event(self._event(event_id="abc"), self.bot)
        self.reactor.callLater.assert_called_once()

    def test_non_runtime_python_changes_do_not_classify_as_core(self) -> None:
        changes = "\n".join(
            (
                "M\tdocs/examples/modules/samplemodule.py",
                "M\tdocker/healthcheck.py",
                "M\tmicroirc_server.py",
                "M\tdbexport.py",
            )
        )
        self.assertEqual(
            updaterelaunch._classify_changes(changes),
            {"core": False, "modules": False, "deps": False, "any": True},
        )

    def test_pending_restart_does_not_starve_module_reloads(self) -> None:
        self.options["auto_restart"] = False
        updaterelaunch._restart_pending.set()
        with (
            patch.object(
                updaterelaunch,
                "_check_and_apply",
                return_value={
                    "core": False,
                    "modules": True,
                    "deps": False,
                    "any": True,
                },
            ),
            patch.object(updaterelaunch, "call_in_reactor") as call,
            patch.object(updaterelaunch, "_restart") as restart,
            patch("builtins.print"),
        ):
            updaterelaunch._event_update_check(self.bot)
        call.assert_called_once_with(updaterelaunch._reload_all)
        restart.assert_not_called()
        self.assertTrue(updaterelaunch._restart_pending.is_set())

    def test_merged_non_runtime_change_is_not_reported_as_up_to_date(self) -> None:
        with (
            patch.object(
                updaterelaunch,
                "_check_and_apply",
                return_value={
                    "core": False,
                    "modules": False,
                    "deps": False,
                    "any": True,
                },
            ),
            patch("builtins.print") as printed,
        ):
            updaterelaunch._event_update_check(self.bot)
        self.assertIn("no runtime code changed", printed.call_args.args[0])
