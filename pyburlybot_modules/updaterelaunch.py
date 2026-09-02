from util.event import Event
from util.types import BotLike
# updaterelaunch super update reload module
#
# Updates are applied on demand: the admin !update command, or an authorized
# "update" event (normally POST <path_prefix>update from the webhook module,
# e.g. a GitHub push webhook). There is deliberately no polling timer.

from collections import deque
from subprocess import STDOUT, CalledProcessError, check_output
from sys import executable
from threading import Event as ThreadEvent
from threading import Lock
from typing import Any

# git fetch
# git diff --name-status main origin/main
# M       stuff
# M       things/abc.txt
# git merge origin/main
from util import Mapping

### Modules should not import this! Unless they have a very good reason to.
from util.settings import Settings
from util.threads import call_in_reactor

### This is only something that modules that know what they are doing should do:
from twisted.internet import reactor as _reactor
###

reactor: Any = _reactor

OPTIONS = {
    "git_path": (str, "Path to git executable.", "git"),
    "git_branch": (str, "Branch tracked for updates.", "main"),
    "update_debounce": (
        int,
        "Seconds to wait after the last 'update' event before checking for updates, "
        "so a burst of pushes causes one update. 0 checks immediately.",
        30,
    ),
    "auto_restart": (
        bool,
        "Allow event-triggered updates to restart the bot when core files change. "
        "If False, core updates are merged but the restart waits for !update or a manual restart. "
        "Module-only updates always hot-reload without disconnecting.",
        True,
    ),
}

GIT_TIMEOUT = 300
PIP_TIMEOUT = 900
_update_lock = Lock()
# set when a core update was merged but auto_restart is disabled;
# the next !update (or event-triggered check with auto_restart enabled) applies it
_restart_pending = ThreadEvent()

# a broadcast "update" event is delivered once per server; remember handled
# event_ids so each post schedules only one check (see reload.py)
_seen_lock = Lock()
_seen_events: deque[str] = deque(maxlen=64)

# python files that never run inside the bot process: changing them must not
# force a restart (matched with str.startswith, so bare names are prefixes)
_NON_RUNTIME_PYTHON_PREFIXES = (
    "tests/",
    "docs/",
    "docker/",
    "microirc",
    "dbexport.py",
)


class _Pending:
    """Debounce state for event-triggered checks. Touched only in the reactor thread."""

    call: Any = None  # twisted DelayedCall
    bot: BotLike | None = None


def _classify_changes(changes: str) -> dict[str, bool]:
    """Classify paths reported by ``git diff --name-status``."""
    result = {"core": False, "modules": False, "deps": False, "any": False}
    for line in changes.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        result["any"] = True
        # Renames and copies report both the old and new path. Classify both so
        # moving a live file out of its runtime location still applies it.
        paths = fields[1:3] if fields[0].startswith(("R", "C")) else fields[1:2]
        for path in paths:
            if path.startswith("pyburlybot_modules/"):
                result["modules"] = True
            elif path == "requirements.txt":
                result["deps"] = True
            elif path.endswith(".py") and not path.startswith(
                _NON_RUNTIME_PYTHON_PREFIXES
            ):
                result["core"] = True
    return result


def _check_and_apply(gitpath: str, branch: str) -> dict[str, bool]:
    """Fetch and fast-forward to origin/<branch>, classifying what changed."""
    check_output(
        [gitpath, "fetch", "origin", branch],
        text=True,
        stderr=STDOUT,
        timeout=GIT_TIMEOUT,
    )
    # diff from HEAD (not the local branch name) so the classification always
    # describes exactly what the merge below applies, even on a detached HEAD
    changes = check_output(
        [gitpath, "diff", "--name-status", "HEAD", "origin/%s" % branch],
        text=True,
        stderr=STDOUT,
        timeout=GIT_TIMEOUT,
    )
    result = _classify_changes(changes)
    if result["any"]:
        print("UPDATERELAUNCH CHANGES:\n%s" % changes)
        # --ff-only matches docker/entrypoint.sh: a diverged or dirty checkout
        # fails here instead of wedging on a conflicted merge
        check_output(
            [gitpath, "merge", "--ff-only", "origin/%s" % branch],
            text=True,
            stderr=STDOUT,
            timeout=GIT_TIMEOUT,
        )
        if result["deps"]:
            print("UPDATERELAUNCH: requirements.txt changed, installing dependencies")
            print(
                check_output(
                    [
                        executable,
                        "-m",
                        "pip",
                        "install",
                        "--no-cache-dir",
                        "-r",
                        "requirements.txt",
                    ],
                    text=True,
                    timeout=PIP_TIMEOUT,
                )
            )
    return result


def _restart() -> None:
    print("RESTARTING BOT")
    call_in_reactor(Settings.shutdown, True)


def _describe_failure(e: Exception) -> str:
    """One line for an update failure, including git/pip output when captured."""
    output = getattr(e, "output", None)
    if output:
        return "%s :: %s" % (e, " ".join(str(output).split()))
    return str(e)


def _notify_admins(bot: BotLike, msg: str) -> None:
    """Best-effort PRIVMSG to each admin so unattended failures reach IRC."""
    try:
        for admin in bot.getOption("admins") or ():
            bot.sendmsg(admin, msg)
    except Exception as e:  # noqa: BLE001 - notification must never mask the failure
        print("UPDATERELAUNCH: could not notify admins: %s" % e)


def update(event: Event, bot: BotLike) -> None:
    """update will check for git update, hot-reload modules or restart bot as needed."""
    gitpath = bot.getOption("git_path", module="updaterelaunch") or "git"
    branch = bot.getOption("git_branch", module="updaterelaunch") or "main"

    if not _update_lock.acquire(blocking=False):
        return bot.say("An update check is already in progress.")
    try:
        result = _check_and_apply(gitpath, branch)
    except (CalledProcessError, OSError) as e:
        return bot.say("Update failed: %s" % _describe_failure(e))
    finally:
        _update_lock.release()

    if result["core"] or result["deps"] or _restart_pending.is_set():
        _restart_pending.clear()
        bot.say("Restarting to apply update...")
        _restart()
    elif result["modules"]:
        call_in_reactor(_reload_all)
        # may never get sent if bot is disconnecting from this server after reload
        bot.say("Module update merged, modules reloaded.")
    elif result["any"]:
        bot.say(
            "Update merged; no runtime code changed "
            "(applies at the next restart or image rebuild)."
        )
    else:
        bot.say("Already up-to date.")


def local_update(event: Event, bot: BotLike) -> None:
    if not bot.getOption("debug"):
        return bot.say("Debug must be enabled for localupdate.")
    bot.say("Restarting...")
    _restart()


def _reload_all() -> None:
    Settings.reloadStage1()
    Settings.reloadStage2()


def _event_update_check(bot: BotLike) -> None:
    """Unattended update check (worker thread) for event-triggered updates."""
    gitpath = bot.getOption("git_path", module="updaterelaunch") or "git"
    branch = bot.getOption("git_branch", module="updaterelaunch") or "main"

    # block (rather than skip) so a trigger that arrives during a running
    # check is never lost: it simply runs right after
    with _update_lock:
        try:
            result = _check_and_apply(gitpath, branch)
        except Exception as e:  # noqa: BLE001 - network/git failures: wait for the next trigger
            detail = _describe_failure(e)
            print("UPDATERELAUNCH: update check failed: %s" % detail)
            _notify_admins(bot, "Unattended update failed: %s" % detail)
            return

    needs_restart = result["core"] or result["deps"] or _restart_pending.is_set()
    if needs_restart and bot.getOption("auto_restart", module="updaterelaunch"):
        print("UPDATERELAUNCH: core update merged, restarting.")
        _restart_pending.clear()
        _restart()
        return
    if needs_restart:
        _restart_pending.set()
        print(
            "UPDATERELAUNCH: core update merged; restart required but "
            "auto_restart is disabled. Use update command or restart manually."
        )
    # a pending restart must not starve module hot-reloads: with auto_restart
    # disabled, module changes still apply while the restart waits for !update
    if result["modules"]:
        print("UPDATERELAUNCH: module update merged, hot-reloading modules.")
        call_in_reactor(_reload_all)
    elif not needs_restart:
        if result["any"]:
            print(
                "UPDATERELAUNCH: update merged; no runtime code changed "
                "(applies at the next restart or image rebuild)."
            )
        else:
            print("UPDATERELAUNCH: already up to date.")


def _fire_pending() -> None:
    # reactor thread: debounce window elapsed
    bot, _Pending.call, _Pending.bot = _Pending.bot, None, None
    if bot is not None:
        reactor.callInThread(_event_update_check, bot)


def _schedule_check(bot: BotLike, delay: float) -> None:
    # reactor thread: (re)start the debounce window; one pending check at most
    _Pending.bot = bot
    if _Pending.call is not None and _Pending.call.active():
        _Pending.call.reset(delay)
    else:
        _Pending.call = reactor.callLater(delay, _fire_pending)


def _github_push_for_branch(event: Event, branch: str) -> bool:
    """Accept GitHub deliveries only when they are a push to ``branch``.

    Non-GitHub events (no X-GitHub-Event header) are accepted as-is.
    """
    headers = getattr(event, "headers", None) or {}
    github_event = headers.get("x-github-event")
    if github_event is None:
        return True
    if github_event != "push":
        print("UPDATERELAUNCH: ignoring GitHub %r event" % github_event)
        return False
    payload = getattr(event, "json", None)
    ref = payload.get("ref") if isinstance(payload, dict) else None
    if ref != "refs/heads/%s" % branch:
        print("UPDATERELAUNCH: ignoring push to %s (tracking %s)" % (ref, branch))
        return False
    if isinstance(payload, dict) and payload.get("deleted"):
        print("UPDATERELAUNCH: ignoring branch deletion push")
        return False
    return True


def update_event(event: Event, bot: BotLike) -> None:
    """Handle a posted "update" event (e.g. webhook: POST <path_prefix>update).

    Requires event.authorized. Checks are debounced (update_debounce) so a
    burst of pushes results in a single fetch/merge/reload.
    """
    source = getattr(event, "remote", None) or "internal"
    if not event.authorized:
        print("UPDATERELAUNCH: ignoring unauthorized update event from %s" % source)
        return
    event_id = getattr(event, "event_id", None)
    if event_id is not None:
        with _seen_lock:
            if event_id in _seen_events:
                return
            _seen_events.append(event_id)
    branch = bot.getOption("git_branch", module="updaterelaunch") or "main"
    if not _github_push_for_branch(event, branch):
        return
    delay = max(0, int(bot.getOption("update_debounce", module="updaterelaunch") or 0))
    print("UPDATERELAUNCH: update requested by %s, checking in %ds" % (source, delay))
    reactor.callFromThread(_schedule_check, bot, delay)


mappings = (
    Mapping(command="update", function=update, admin=True),
    Mapping(command="localupdate", function=local_update, admin=True),
    Mapping(types=["update"], function=update_event),
)
