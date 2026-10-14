"""Canonical recipe inputs shared by calibration and runtime correlation."""

import inspect
import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any

from pydantic import TypeAdapter

from specgate.context import ContextPacket
from specgate.shared.domain.inputs import Item
from specgate.slices.check import check
from specgate.slices.decide import decide
from specgate.slices.equivalence import equivalence
from specgate.slices.find import find
from specgate.slices.screen import screen
from specgate.slices.verify import verify


def recipe_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Use the recipe's actual defaults for execution and question identity."""
    values = dict(arguments)
    if values.get("context") is not None:
        values["context"] = TypeAdapter(ContextPacket).validate_python(
            values["context"]
        )
    for key in ("claims", "candidates", "options"):
        if key in values:
            values[key] = [Item.model_validate(item) for item in values[key]]
    recipes: dict[str, Callable[..., Any]] = {
        "jev_verify": verify,
        "jev_screen": screen,
        "jev_find": find,
        "jev_decide": decide,
        "jev_check": check,
        "jev_equivalence": equivalence,
    }
    try:
        bound = inspect.signature(recipes[tool]).bind(**values, client=None)
    except TypeError:
        raise ValueError(
            "Argumentos incompatíveis com a receita de calibração."
        ) from None
    bound.apply_defaults()
    for key in ("auto_accept", "block_at", "review_at", "defect_at"):
        if type(bound.arguments.get(key)) in (int, float):
            bound.arguments[key] = float(bound.arguments[key])
    del bound.arguments["client"]
    return dict(bound.arguments)


def request_revision(tool: str, arguments: dict[str, Any]) -> str:
    canonical = TypeAdapter(dict[str, Any]).dump_python(
        recipe_arguments(tool, arguments), mode="json"
    )
    return sha256(
        json.dumps(
            [tool, canonical], sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
