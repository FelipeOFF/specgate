import json
import math
from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from specgate.context import ContextPacket
from specgate.privacy import ensure_safe_content


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=16000)

    @field_validator("id", "text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ID e texto não podem ficar vazios.")
        return value


def item_map(items: list[Item]) -> dict[str, str]:
    if not 1 <= len(items) <= 50:
        raise ValueError("Envie entre 1 e 50 itens.")
    result: dict[str, str] = {}
    for item in items:
        if item.id in result:
            raise ValueError("ID público duplicado.")
        result[item.id] = item.text
    if sum(len(text.encode()) for text in result.values()) > 64000:
        raise ValueError("Itens excedem o limite de 64 KB; divida a avaliação.")
    return result


def checked_text(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError("Informe um texto não vazio.")
    if len(value.encode()) > 64000:
        raise ValueError("Texto excede o limite de 64 KB; divida a avaliação.")
    ensure_safe_content(value)
    return value


def threshold(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Threshold deve ser um número finito entre 0 e 1.")


def closed_gate_review(selected: Any, label: Any, reason: Any) -> str | None:
    """Name the option a closed gate withheld."""
    if not isinstance(selected, str) or not selected.strip():
        return None
    shown = label.strip() if isinstance(label, str) and label.strip() else selected
    named = shown if shown == selected else f"{shown} ({selected})"
    detail = reason.strip() if isinstance(reason, str) and reason.strip() else "gate_closed"
    return (
        f"Jev escolheu {named}. "
        f"O gate ficou fechado: {detail}. "
        "Confirme essa opção ou escolha outra."
    )


def review_metadata(mode: str, *responses: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": mode,
        "calibrated": False,
        "auto_advance": False,
        "action": "review",
        "reason": "real_calibration_pending",
        "provider_calls": [r["metadata"] for r in responses if "metadata" in r],
    }


def attach_context(
    state: dict[str, Any], context: ContextPacket | None
) -> dict[str, Any]:
    if context is not None:
        if context.gaps or context.conflicts or context.unexamined:
            raise ValueError("Resolva as lacunas de contexto antes de avaliar.")
        state["context"] = asdict(context)
    checked_text(json.dumps(state, ensure_ascii=False, allow_nan=False))
    return state
