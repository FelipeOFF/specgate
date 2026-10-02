"""Per-claim scores and evidence bindings for versioned specifications."""

import math
from typing import Any

# Jev reads claims literally: an option in the present tense would not match a
# spec that states it as a change. The scope claim stays raw; framing it scored lower.
DECISION_FRAME = "A spec adota esta decisão do grill como mudança a implementar: "


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
            *[
                {"id": f"decision-{index}", "text": text}
                for index, text in enumerate(draft["implementation_decisions"], 1)
            ],
        ],
        "coverage": coverage,
    }


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
        return ["Jev: verification_incomplete"]
    for kind in ("fidelity", "coverage"):
        result = verification.get(kind, {})
        if result.get("error") or not result.get("verdicts"):
            return ["Jev: verification_incomplete"]
        claims = verification.get("claims", {}).get(kind)
        if claims is not None and (
            len(result["verdicts"]) != len(claims)
            or {row.get("id") for row in result["verdicts"]}
            != {claim["id"] for claim in claims}
        ):
            return ["Jev: verification_incomplete"]
    return [f"Jev: {claim}" for claim in weak_claims(verification, threshold)]
