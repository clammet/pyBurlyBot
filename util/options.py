from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Option:
    """Metadata and default value for a configurable option."""

    type: type[Any]
    description: str
    default: Any
    secret: bool = False
    writeonly: bool = False

    def __post_init__(self) -> None:
        if self.writeonly and not self.secret:
            raise ValueError("A write-only option must also be secret.")


def option_spec(value: Option | tuple[type[Any], str, Any]) -> Option:
    """Normalize the historic three-tuple declaration into an Option."""

    if isinstance(value, Option):
        return value
    if not isinstance(value, tuple) or len(value) != 3:
        raise ValueError("Expected Option or (type, description, default).")
    option_type, description, default = value
    return Option(option_type, description, default)
