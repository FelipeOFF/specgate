"""Typed report for a request above the size limits; it never carries payload content."""

import functools
import inspect
import json
import logging
import traceback
from collections.abc import Callable
from types import FrameType
from typing import Any, cast

from specgate.shared.domain import inputs

CODE = "payload_too_large"
# Same bound as shared.domain.inputs; the tests pin the two together.
LIMIT = 64000
# The host reads at most this many bytes of one HTTP request and refuses more with a
# bare 413 that the client cannot tell from a transport failure. The client measures
# the arguments first, leaving a margin for the JSON-RPC envelope around them.
BODY_LIMIT = 262144
ARGUMENT_LIMIT = BODY_LIMIT - 1024

logger = logging.getLogger("specgate.payload")


class PayloadTooLarge(ValueError):
    def __init__(self, size: int, limit: int = LIMIT) -> None:
        super().__init__(
            f"O payload tem {size} bytes e o limite é {limit}; "
            "reduza o conteúdo e envie de novo."
        )
        self.code, self.size, self.limit = CODE, size, limit

    def result(self) -> dict[str, Any]:
        """The tool result: sizes and a fixed message, never the payload."""
        return {
            "action": "needs_human",
            "auto_advance": False,
            "error": {
                "code": CODE,
                "message": str(self),
                "size": self.size,
                "limit": self.limit,
            },
        }

    def review(self, state: dict[str, Any]) -> dict[str, Any]:
        """Hold the state for human review, with the typed error attached."""
        return {**state, **self.result()}

    @classmethod
    def from_result(cls, result: Any) -> "PayloadTooLarge | None":
        """Recognise the host's typed error in a tool result."""
        error = result.get("error") if isinstance(result, dict) else None
        if not isinstance(error, dict) or error.get("code") != CODE:
            return None
        size, limit = error.get("size"), error.get("limit")
        if type(size) is not int or type(limit) is not int:
            return None
        return cls(size, limit)


def request_overflow(arguments: dict[str, Any]) -> PayloadTooLarge | None:
    """The overflow of a request the host would refuse at the HTTP body."""
    try:
        size = len(
            json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
        )
    except (TypeError, ValueError):
        return None
    return PayloadTooLarge(size, ARGUMENT_LIMIT) if size > ARGUMENT_LIMIT else None


def _measured(frame: FrameType) -> int | None:
    # The recipes hash inputs.py into their revision, so it keeps raising a bare
    # ValueError; what it measured is read back from the frame that failed.
    code, local = frame.f_code, frame.f_locals
    if code is inputs.checked_text.__code__:
        value = local.get("value")
        return len(value.encode()) if isinstance(value, str) else None
    if code is inputs.item_map.__code__:
        result, items = local.get("result"), local.get("items")
        if isinstance(result, dict) and len(result) == len(items or ()):
            return sum(len(text.encode()) for text in result.values())
    return None


def oversize(error: BaseException) -> PayloadTooLarge | None:
    """The overflow behind this error, when the 64 KB check raised it."""
    if isinstance(error, BaseExceptionGroup):
        found = [oversize(item) for item in error.exceptions]
        return found[0] if found and all(found) else None
    if not isinstance(error, ValueError):
        return None
    for frame, _ in traceback.walk_tb(error.__traceback__):
        size = _measured(frame)
        if size is not None and size > LIMIT:
            return PayloadTooLarge(size)
    return None


def typed[F: Callable[..., Any]](tool: F) -> F:
    """Return the typed error instead of letting an overflow crash the tool."""

    def report(error: BaseException) -> dict[str, Any] | None:
        found = oversize(error)
        if found is None:
            return None
        logger.warning(
            "%s tool=%s size=%d limit=%d", CODE, tool.__name__, found.size, found.limit
        )
        return found.result()

    if inspect.iscoroutinefunction(tool):

        @functools.wraps(tool)
        async def guarded_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await tool(*args, **kwargs)
            except (ValueError, ExceptionGroup) as error:
                if (reported := report(error)) is None:
                    raise
                return reported

        return cast(F, guarded_async)

    @functools.wraps(tool)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return tool(*args, **kwargs)
        except ValueError as error:
            if (reported := report(error)) is None:
                raise
            return reported

    return cast(F, guarded)
