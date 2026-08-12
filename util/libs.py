from collections.abc import Iterable, Iterator, MutableSet
from typing import Any, TypeVar

T = TypeVar("T")

class OrderedSet(MutableSet[T]):
	# http://code.activestate.com/recipes/576694/
	def __init__(self, iterable: Iterable[T] | None=None) -> None:
		end: list[Any] = []
		self.end = end
		end += [None, end, end]         # sentinel node for doubly linked list
		self.map: dict[T, list[Any]] = {} # key --> [key, prev, next]
		if iterable is not None:
			for item in iterable:
				self.add(item)

	def __len__(self) -> int:
		return len(self.map)

	def __contains__(self, key: object) -> bool:
		return key in self.map

	def add(self, key: T) -> None:
		if key not in self.map:
			end = self.end
			curr = end[1]
			curr[2] = end[1] = self.map[key] = [key, curr, end]

	def discard(self, key: T) -> None:
		if key in self.map:        
			key, prev, next = self.map.pop(key)
			prev[2] = next
			next[1] = prev

	def __iter__(self) -> Iterator[T]:
		end = self.end
		curr = end[2]
		while curr is not end:
			yield curr[0]
			curr = curr[2]

	def __reversed__(self) -> Iterator[T]:
		end = self.end
		curr = end[1]
		while curr is not end:
			yield curr[0]
			curr = curr[1]

	def pop(self, last: bool=True) -> T:
		if not self:
			raise KeyError('set is empty')
		key = self.end[1][0] if last else self.end[2][0]
		self.discard(key)
		return key

	def __repr__(self) -> str:
		if not self:
			return '%s()' % (self.__class__.__name__,)
		return '%s(%r)' % (self.__class__.__name__, list(self))

	def __eq__(self, other: object) -> bool:
		if isinstance(other, OrderedSet):
			return len(self) == len(other) and list(self) == list(other)
		if isinstance(other, Iterable):
			return set(self) == set(other)
		return False
