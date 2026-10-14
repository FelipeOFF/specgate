"""Per-claim scores and evidence bindings for versioned specifications."""

import math
from collections.abc import Sequence
from typing import Any

from specgate.decision_ids import (
    CLAIM_PREFIX,
    claim_id,
    decision_id,
    decision_ids,
    keeps_id,
)
from specgate.decision_lineage import Group
from specgate.lineage_resolution import Resolution, resolve

# Jev reads claims literally: an option in the present tense would not match a
# spec that states it as a change. The scope claim stays raw; framing it scored lower.
DECISION_FRAME = "A spec adota esta decisão do grill como mudança a implementar: "
# The gap of a verification that does not judge every claim it lists.
INCOMPLETE = "Jev: verification_incomplete"


def expected_claims(
    draft: dict[str, Any],
    objective: str,
    decisions: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    coverage = [{"id": "scope", "text": objective}]
    for turn in decisions:
        answer = turn["answer"]
        selected = next(
            (
                option["text"]
                for option in turn["question"]["options"]
                if option["id"] == answer.get("selected_option")
            ),
            answer.get("text"),
        )
        if selected:
            coverage.append({"id": turn["id"], "text": DECISION_FRAME + selected})
    return {
        "fidelity": [
            {"id": "problem", "text": draft["problem_statement"]},
            {"id": "solution", "text": draft["solution"]},
            # The id follows the decision, not its position, so a division, an insertion or
            # a removal moves no other id. Two decisions with one id are refused.
            *[
                {"id": claim_id(identifier), "text": text}
                for identifier, text in zip(
                    decision_ids(draft["implementation_decisions"]),
                    draft["implementation_decisions"],
                    strict=True,
                )
            ],
        ],
        "coverage": coverage,
    }


def wording_targets(claims: Any) -> tuple[list[tuple[str, str]], str]:
    """The texts the agent wrote that the wording check covers, and the scope it checks them in.

    The targets are the implementation decisions, by the id of their fidelity claim. The
    scope is the objective of the grill and its active decisions, as the coverage claims
    carry them. `claims` is the shape of `expected_claims`, so the client, the host when
    it stores a verification and the host when it reads one derive the same pair.
    """
    groups = claims if isinstance(claims, dict) else {}

    def rows(kind: str) -> list[dict[str, Any]]:
        found = groups.get(kind)
        return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []

    targets = [
        (row["id"], row["text"])
        for row in rows("fidelity")
        if isinstance(row.get("id"), str)
        and row["id"].startswith(CLAIM_PREFIX)
        and isinstance(row.get("text"), str)
    ]
    texts = [row["text"] for row in rows("coverage") if isinstance(row.get("text"), str)]
    if not texts:
        return targets, ""
    objective, *chosen = texts
    lines = [f"Objetivo do grill: {objective}"]
    if chosen:
        lines += ["Decisões ativas do grill:", *(f"- {text}" for text in chosen)]
    return targets, "\n".join(lines)


def derived_targets(claims: Any) -> list[tuple[str, str]]:
    """The claims the host derived that the derivation check covers, by `coverage:<id>`.

    These are the scope and one claim per active decision of the grill. The agent wrote
    none of them, so a defect found in one is a diagnostic for the maintainer, never a
    request to rewrite. The fidelity claims are not here: `problem` and `solution` are not
    derived, and the decisions the agent wrote are checked by `wording_targets`. The
    shape is the one of `expected_claims`, as in `wording_targets`.
    """
    found = claims.get("coverage") if isinstance(claims, dict) else None
    return [
        (f"coverage:{row['id']}", row["text"])
        for row in (found if isinstance(found, list) else [])
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and isinstance(row.get("text"), str)
    ]


def keeps_researched_decision(stored: dict[str, Any], repaired: dict[str, Any], claim: str) -> None:
    """Refuse a repair that takes the id of the decision a research cycle is about.

    `claim` is the claim of the cycle (`fidelity:decision-<id>`); any other claim is a
    repair that cannot change an id. Raises the typed refusal of `decision_id_changed`.
    """
    prefix = "fidelity:" + CLAIM_PREFIX
    if claim.startswith(prefix):
        keeps_id(
            claim.removeprefix(prefix),
            stored.get("implementation_decisions") or [],
            repaired.get("implementation_decisions") or [],
            repaired.get("decision_lineage") or {},
        )


def decision_texts(draft: Any) -> dict[str, str]:
    """The implementation decisions of a stored or proposed draft, by stable id."""
    texts = draft.get("implementation_decisions") if isinstance(draft, dict) else None
    return (
        {decision_id(text): text for text in texts if isinstance(text, str)}
        if isinstance(texts, list)
        else {}
    )


def earlier_specs(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The stored revisions of the spec, newest first: the one on the run, then its history."""
    stored = state.get("spec")
    history = state.get("spec_history")
    return [
        *([stored] if isinstance(stored, dict) else []),
        *(reversed(history) if isinstance(history, list) else []),
    ]


def spec_resolution(
    draft: dict[str, Any],
    earlier: Sequence[dict[str, Any]],
    record: Any = None,
    *,
    strict: bool = True,
) -> Resolution:
    """The lineage of a draft against the stored revisions before it, newest first.

    What the draft declares and what it inherits from the previous revision (a part whose
    text did not change and whose equivalence was not accepted). A strict resolution raises
    the typed refusal of `decision_lineage` when an entry cannot be resolved.
    """
    return resolve(draft, earlier, decision_texts, "decision_lineage", record, strict=strict)


def spec_groups(draft: dict[str, Any], earlier: Sequence[dict[str, Any]]) -> list[Group]:
    """The groups of parts and originals the lineage of a draft names.

    `earlier` are the stored revisions before the draft, newest first. Raises the typed
    refusal of `decision_lineage` when an entry cannot be resolved.
    """
    return spec_resolution(draft, earlier).groups


def strong(row: dict[str, Any], threshold: float) -> bool:
    values = (row.get("confidence"), row.get("probabilities", {}).get("supports"))
    return row.get("verdict") == "verified" and all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and max(0.8, threshold) < value <= 1
        for value in values
    )


def weak_claims(verification: dict[str, Any], threshold: float) -> list[str]:
    return [
        f"{kind}:{row['id']}"
        for kind in ("fidelity", "coverage")
        for row in verification.get(kind, {}).get("verdicts", [])
        if not strong(row, threshold)
    ]


def verification_gaps(verification: dict[str, Any], threshold: float) -> list[str]:
    if not verification:
        return [INCOMPLETE]
    for kind in ("fidelity", "coverage"):
        result = verification.get(kind, {})
        if result.get("error") or not result.get("verdicts"):
            return [INCOMPLETE]
        claims = verification.get("claims", {}).get(kind)
        if claims is not None and (
            len(result["verdicts"]) != len(claims)
            or {row.get("id") for row in result["verdicts"]}
            != {claim["id"] for claim in claims}
        ):
            return [INCOMPLETE]
    return [f"Jev: {claim}" for claim in weak_claims(verification, threshold)]
