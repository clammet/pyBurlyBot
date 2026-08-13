from collections.abc import Callable, Sequence
from re import Pattern
from typing import Any

from .event import Event
from .types import BotLike

MappingFunction = Callable[[Event, BotLike], Any]


def dummyfunc(event: Event, botwrap: BotLike) -> None:
    pass


# TODO: Should this be called "hook"? (with the variable name in modules called "hooks")
#     "hooks" seems kind of "low level" though...
class Mapping:
    def __init__(
        self,
        types: Sequence[str] | None = None,
        command: str | Sequence[str] | None = None,
        regex: Pattern[str] | None = None,
        function: MappingFunction = dummyfunc,
        priority: int = 10,
        override: bool = False,
        admin: bool = False,
        hidden: bool = False,
    ) -> None:
        """Mapping object to map module functions to IRC events.
        Mapping takes the following arguments:
        type = [list of strings],
        command = string|[listofcommands],
        regex = compiledRegExobject,
        function = a defined function should be expecting the following arguments:
                def dummyfunc(event, botwrap):
                command arg can be a list of commands,
        priority = priority for dispatch ordering (Not really useful since module functions are called in
                a thread pool.
        override = If True will override internal bot routines (currently only implemented on sendmsg.)
                If False, internal bot routines will run as well as the event being dispatched. (Default)
        admin = If True, only dispatch when invoked by an admin user
        hidden = If True, do not display in standard command listing, e.g. help
                If False behave normally with regards to listing
        """
        if types is not None and not isinstance(types, (list, tuple)):
            raise TypeError("Mapping types must be a list or tuple.")
        if not types:
            self.types: list[str] = []
        else:
            self.types = list(types)

        if command is not None and not isinstance(command, (list, tuple, str)):
            raise TypeError("Mapping command must be a string, list, or tuple.")
        if command:
            if isinstance(command, str):
                command = [command]
            if not self.types:
                self.types = ["privmsged"]
        self.command: list[str] | None = list(command) if command else None
        self.regex = regex
        self.function = function
        self.priority = priority
        self.override = override
        self.admin = admin
        self.hidden = hidden

    def __repr__(self) -> str:
        return (
            "Mapping(id()=%X, types=%r, command=%r, regex=%r, function=%r, priority=%r, admin=%r)"
            % (
                id(self),
                self.types,
                self.command,
                self.regex,
                self.function,
                self.priority,
                self.admin,
            )
        )
