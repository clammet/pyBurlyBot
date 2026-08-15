from util.event import Event
from util.types import BotLike
# updaterelaunch super update reload module

from subprocess import CalledProcessError, check_output
from sys import executable
from threading import Event as ThreadEvent
from threading import Lock

# git fetch
# git diff --name-status main origin/main
# M       stuff
# M       things/abc.txt
# git merge origin/main
from util import Mapping, TimerExists, TimerNotFound, Timers

### Modules should not import this! Unless they have a very good reason to.
from util.settings import Settings

### This is only something that modules that know what they are doing should do:
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread
###

OPTIONS = {
    "git_path": (str, "Path to git executable.", "git"),
    "git_branch": (str, "Branch tracked for updates.", "main"),
    "update_interval": (
        int,
        "Seconds between automatic update checks. 0 disables. Takes effect on reload/restart.",
        0,
    ),
    "auto_restart": (
        bool,
        "Allow automatic update checks to restart the bot when core files change. "
        "If False, core updates are merged but the restart waits for !update or a manual restart. "
        "Module-only updates always hot-reload without disconnecting.",
        True,
    ),
}

GIT_TIMEOUT = 300
PIP_TIMEOUT = 900
_TIMER_NAME = "updaterelaunch-autoupdate"
_update_lock = Lock()
# set when a core update was merged but auto_restart is disabled;
# the next !update (or auto check with auto_restart enabled) applies it
_restart_pending = ThreadEvent()


# TODO: This won't really play nice when running multiple bot processes at a time.
#     After the first bot process updates, the rest will think they are up-to-date.
#     This could be solved by storing modtimes of modules and core files at launch time and comparing them.
def _check_and_apply(gitpath: str, branch: str) -> dict[str, bool]:
    """Fetch and merge origin/<branch>, classifying what changed."""
    check_output([gitpath, "fetch"], text=True, timeout=GIT_TIMEOUT)
    changes = check_output(
        [gitpath, "diff", "--name-status", branch, "origin/%s" % branch],
        text=True,
        timeout=GIT_TIMEOUT,
    )
    result = {"core": False, "modules": False, "deps": False, "any": False}
    for line in changes.splitlines():
        if "\t" not in line:
            continue
        _status, path = line.split("\t", 1)
        result["any"] = True
        if path.startswith("pyburlybot_modules/"):
            result["modules"] = True
        elif path == "requirements.txt":
            result["deps"] = True
        elif path.endswith(".py"):
            result["core"] = True
    if result["any"]:
        print("UPDATERELAUNCH CHANGES:\n%s" % changes)
        check_output(
            [gitpath, "merge", "origin/%s" % branch], text=True, timeout=GIT_TIMEOUT
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
    blockingCallFromThread(reactor, Settings.shutdown, True)


def update(event: Event, bot: BotLike) -> None:
    """update will check for git update, hot-reload modules or restart bot as needed."""
    gitpath = bot.getOption("git_path", module="updaterelaunch") or "git"
    branch = bot.getOption("git_branch", module="updaterelaunch") or "main"

    if not _update_lock.acquire(blocking=False):
        return bot.say("An update check is already in progress.")
    try:
        result = _check_and_apply(gitpath, branch)
    except (CalledProcessError, OSError) as e:
        return bot.say("Update failed: %s" % e)
    finally:
        _update_lock.release()

    if result["core"] or result["deps"] or _restart_pending.is_set():
        _restart_pending.clear()
        bot.say("Restarting to apply update...")
        _restart()
    elif result["modules"]:
        # reload
        if bot.isModuleAvailable("reload"):
            bot.getModule("reload").admin_reload_bot(event, bot)
        else:
            bot.say("Module(s) updated but can't reload. reload module not available.")
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


def _auto_update_check(bot: BotLike) -> None:
    """Timer callback (runs in a worker thread): unattended update check."""
    gitpath = bot.getOption("git_path", module="updaterelaunch") or "git"
    branch = bot.getOption("git_branch", module="updaterelaunch") or "main"

    if not _update_lock.acquire(blocking=False):
        return
    try:
        result = _check_and_apply(gitpath, branch)
    except Exception as e:  # noqa: BLE001 - network/git failures just wait for the next tick
        print("UPDATERELAUNCH: automatic update check failed: %s" % e)
        return
    finally:
        _update_lock.release()

    if result["core"] or result["deps"] or _restart_pending.is_set():
        if bot.getOption("auto_restart", module="updaterelaunch"):
            print("UPDATERELAUNCH: core update merged, restarting.")
            _restart_pending.clear()
            _restart()
        else:
            _restart_pending.set()
            print(
                "UPDATERELAUNCH: core update merged; restart required but "
                "auto_restart is disabled. Use update command or restart manually."
            )
    elif result["modules"]:
        print("UPDATERELAUNCH: module update merged, hot-reloading modules.")
        blockingCallFromThread(reactor, _reload_all)


def init(bot: BotLike) -> bool:
    interval = bot.getOption("update_interval", module="updaterelaunch")
    if interval and int(interval) > 0:
        try:
            Timers.addtimer(
                _TIMER_NAME, int(interval), _auto_update_check, reps=-1, bot=bot
            )
        except TimerExists:
            # init runs once per server; a single timer covers all of them
            pass
    return True


def unload() -> None:
    # drop the timer so a module reload never leaves a stale callback behind
    try:
        Timers.deltimer(_TIMER_NAME)
    except TimerNotFound:
        pass


mappings = (
    Mapping(command="update", function=update, admin=True),
    Mapping(command="localupdate", function=local_update, admin=True),
)
