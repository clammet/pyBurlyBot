"""Shared typing contracts for the dynamically loaded bot modules."""
from collections.abc import Callable
from typing import Any, Protocol, TypeAlias


DatabaseParams: TypeAlias = tuple[Any, ...] | list[Any] | dict[str, Any]
DatabaseQuery: TypeAlias = Callable[..., Any]


class BotLike(Protocol):
	"""Interface exposed to command handlers and module lifecycle hooks.

	The concrete object can be a :class:`BotWrapper`, a setup container, or a
	module-specific adapter.  Attribute fallback is intentional because addons
	and IRC operations are attached dynamically.
	"""

	network: str

	def __getattr__(self, name: str) -> Any:
		"""Return a dynamically exposed bot operation or attribute."""
		...

	def say(self, msg: Any, **kwargs: Any) -> Any:
		"""Send a response to the current event source."""
		...

	def getOption(self, opt: str, **kwargs: Any) -> Any:
		"""Read one resolved configuration option."""
		...

	def setOption(self, opt: str, value: Any, **kwargs: Any) -> Any:
		"""Set one resolved configuration option."""
		...
