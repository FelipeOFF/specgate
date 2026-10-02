"""Complete planning obligations derived from the approved spec and ticket graph."""

import json
import math
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+(?=[A-ZÀ-Ý])")
LIST_MARKER = re.compile(r"^(?:- |\d+\. )")
OBLIGATION_PREFIX = (
    "O planejamento cobre esta obrigação ou respeita este limite da spec aprovada: "
)
# Problem sentences state current behavior: the plan resolves it, never keeps it.
PROBLEM_PREFIX = "O planejamento resolve este problema descrito na spec aprovada: "
PROBLEM_SECTION = "Problem Statement"
# Exclusions assert about the graph: a frame true from the spec text alone let
# plans doing the excluded item pass. Items become the excluded action.
# Only one-sentence items were validated: a justification sentence after the
# item is framed as excluded too and can hold a correct plan for review.
OUT_OF_SCOPE_PREFIX = (
    "Nenhum ticket do grafo proposto realiza o que a spec aprovada deixa fora do escopo: "
)
OUT_OF_SCOPE_SECTION = "Out of Scope"
# Metadata is skipped only as the last section: a field can carry a heading of
# any name, and one in the middle of the body must not hide the items after it.
METADATA_SECTIONS = frozenset({"Sources and Revision"})
# Only these close a block without being text: ATX headings (a '#' not followed
# by a space, like '#124', is text) and what the host appends to the body.
HEADING = re.compile(r"#{1,6}(?:\s|$)")
APPENDED = re.compile(
    r"<!-- dev-decision-[\w-]+:[\w:.-]+ -->|Origem: grill [\w-]+, revisão [\w-]+\."
)
# Symmetric confirmation: a claim this close to the cutoff, above or below, is
# evaluated once more with the same input and decided by the mean of the two.
CONFIRMATION_BAND = Decimal("0.05")
CONFIRMATION_MISMATCH = "Jev: verification_confirmation_mismatch"


def planning_evidence(
    spec: dict[str, Any], draft: dict[str, Any], sources: list[dict[str, Any]]
) -> str:
    return json.dumps(
        {
            "evaluation_scope": "Planning quality, not completed implementation. Check every obligation and real prerequisite against the proposed graph.",
            "approved_spec": spec["body"],
            "spec_revision": spec["revision"],
            "graph": draft,
            "research": sources,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _blocks(spec_body: str) -> Iterator[tuple[str | None, int, str]]:
    """Paragraphs and list items as (section, first line number, joined text)."""
    body = spec_body.splitlines()
    final = max(
        (number for number, line in enumerate(body, 1) if line.startswith("## ")),
        default=0,
    )
    section: str | None = None
    metadata = False
    start = 0
    lines: list[str] = []
    for number, line in enumerate(body, 1):
        boundary = (
            not line.strip()
            or HEADING.match(line) is not None
            or APPENDED.fullmatch(line.rstrip()) is not None
        )
        if lines and (boundary or LIST_MARKER.match(line)):
            yield section, start, " ".join(lines)
            lines = []
        if line.startswith("## "):
            section = line[3:].strip()
            metadata = number == final and section in METADATA_SECTIONS
        elif not boundary and not metadata:
            if not lines:
                start = number
            lines.append(line.strip())
    if lines:
        yield section, start, " ".join(lines)


def _excluded(sentence: str) -> str:
    item = sentence.removeprefix("Não ").removeprefix("não ")
    return item[:1].lower() + item[1:]


def _coverage(section: str | None, sentence: str) -> str:
    if section == PROBLEM_SECTION:
        return PROBLEM_PREFIX + sentence
    if section == OUT_OF_SCOPE_SECTION:
        return OUT_OF_SCOPE_PREFIX + _excluded(sentence)
    return OBLIGATION_PREFIX + sentence


def expected_claims(spec_body: str, draft: dict[str, Any]) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for section, start, text in _blocks(spec_body):
        sentences = SENTENCE_BREAK.split(LIST_MARKER.sub("", text, count=1).strip())
        claims.extend(
            {"id": f"coverage:{start}:{part}", "text": _coverage(section, sentence)}
            for part, sentence in enumerate(sentences, 1)
        )
    for ticket in draft["tickets"]:
        claims.extend(
            [
                {
                    "id": f"verticality:{ticket['id']}",
                    "text": f"O ticket {ticket['id']} entrega comportamento observável vertical com critérios verificáveis, sem ser apenas uma camada técnica isolada.",
                },
                {
                    "id": f"blockers:{ticket['id']}",
                    "text": f"O ticket {ticket['id']} declara todos e somente os bloqueadores necessários ao comportamento e critérios propostos, considerando a spec e as fontes autorizadas.",
                },
            ]
        )
    claims.append(
        {
            "id": "order",
            "text": "A ordem topológica permite entregar e validar cada comportamento sem depender de trabalho ausente ou posterior; relações entre repos permanecem dependências do grafo.",
        }
    )
    return claims


def _cutoff(threshold: float) -> Decimal:
    # Decimal of the shortest repr keeps "strictly above" exact at 0.80.
    return Decimal(repr(max(0.8, threshold)))


def _unit(value: Any) -> Decimal | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        return None
    return Decimal(repr(value))


def _scores(row: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    probabilities = row.get("probabilities")
    support = probabilities.get("supports") if isinstance(probabilities, dict) else None
    return _unit(row.get("confidence")), _unit(support)


def effective_cutoff(threshold: float) -> float:
    return float(_cutoff(threshold))


def scored(row: dict[str, Any]) -> bool:
    """Both confidence and support are valid scores."""
    return all(value is not None for value in _scores(row))


def strong(row: dict[str, Any], threshold: float) -> bool:
    cutoff = _cutoff(threshold)
    return row.get("verdict") == "verified" and all(
        value is not None and value > cutoff for value in _scores(row)
    )


def near(row: dict[str, Any], threshold: float) -> bool:
    """Confidence or support within the band of the cutoff, above or below."""
    cutoff = _cutoff(threshold)
    return any(
        value is not None and abs(value - cutoff) <= CONFIRMATION_BAND
        for value in _scores(row)
    )


def _mean(
    first: dict[str, Any], second: dict[str, Any]
) -> tuple[Decimal, Decimal] | None:
    (confidence_one, support_one), (confidence_two, support_two) = (
        _scores(first),
        _scores(second),
    )
    if (
        confidence_one is None
        or support_one is None
        or confidence_two is None
        or support_two is None
    ):
        return None
    return (confidence_one + confidence_two) / 2, (support_one + support_two) / 2


def decided(
    first: dict[str, Any], second: dict[str, Any] | None, threshold: float
) -> bool:
    """Whether the claim clears the gate, from the raw rows alone.

    Outside the band the first evaluation decides. Inside it the claim needs the
    second evaluation: both verified, with each mean strictly above the cutoff.
    """
    if not near(first, threshold):
        return strong(first, threshold)
    if second is None:
        return False
    mean = _mean(first, second)
    return (
        mean is not None
        and first.get("verdict") == second.get("verdict") == "verified"
        and all(value > _cutoff(threshold) for value in mean)
    )


def _requests(verification: dict[str, Any]) -> dict[str, Any]:
    """The provider request each claim's first evaluation was judged on."""
    results = verification.get("results")
    return {
        row["id"]: result.get("request_revision")
        for result in (results if isinstance(results, list) else [])
        if isinstance(result, dict) and isinstance(result.get("verdicts"), list)
        for row in result["verdicts"]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def confirmation_rows(verification: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Second evaluations by claim ID; None when they are not well formed.

    A claim has at most one, so a weak claim cannot be evaluated until it passes.
    Each one must come from the same provider request as the first evaluation of
    its claim: a different input would not make the mean of two samples.
    """
    results = verification.get("confirmations", [])
    if not isinstance(results, list):
        return None
    requests = _requests(verification)
    rows: dict[str, dict[str, Any]] = {}
    for result in results:
        verdicts = result.get("verdicts") if isinstance(result, dict) else None
        if not isinstance(verdicts, list) or result.get("error"):
            return None
        request = result.get("request_revision")
        for row in verdicts:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                return None
            if row["id"] in rows:
                return None
            if not isinstance(request, str) or not request:
                return None
            if requests.get(row["id"]) != request:
                return None
            rows[row["id"]] = row
    return rows


def first_rows(verification: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for result in verification.get("results", [])
        if isinstance(result, dict)
        for row in result.get("verdicts", [])
        if isinstance(row, dict)
    ]


def _evaluation(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("verdict", "confidence", "probabilities")}


def evaluations_of(
    first: dict[str, Any], second: dict[str, Any] | None
) -> dict[str, Any]:
    """The evaluations of a claim and their mean, from its raw rows."""
    mean = _mean(first, second) if second else None
    return {
        "evaluations": [_evaluation(first), *([_evaluation(second)] if second else [])],
        "mean": {"confidence": float(mean[0]), "supports": float(mean[1])}
        if mean
        else None,
    }


def confirmation_record(
    verification: dict[str, Any], threshold: float
) -> dict[str, Any] | None:
    """Both evaluations, the mean and the rule, recomputed from the raw rows."""
    second = confirmation_rows(verification) or {}
    claims = []
    for row in first_rows(verification):
        if not near(row, threshold) or not isinstance(row.get("id"), str):
            continue
        other = second.get(row["id"])
        claims.append(
            {
                "id": row["id"],
                **evaluations_of(row, other),
                "strong": decided(row, other, threshold),
            }
        )
    if not claims:
        return None
    return {
        "rule": {
            "name": "symmetric_confirmation",
            "threshold": effective_cutoff(threshold),
            "band": float(CONFIRMATION_BAND),
            "evaluations": 2,
            "applies_to": "confidence or support within the band of the threshold, above or below",
            "decision": "both evaluations verified and the mean of confidence and the mean of support strictly above the threshold",
        },
        "claims": claims,
    }


def with_confirmation(verification: dict[str, Any], threshold: float) -> dict[str, Any]:
    """Replace any client-provided record with the one the server computes."""
    raw = {key: value for key, value in verification.items() if key != "confirmation"}
    record = confirmation_record(raw, threshold)
    return {**raw, "confirmation": record} if record else raw


def gaps(
    verification: dict[str, Any], expected: list[dict[str, str]], threshold: float
) -> list[str]:
    if verification.get("claims") != expected:
        return ["Jev: verification_claim_mismatch"]
    rows = first_rows(verification)
    if (
        len(rows) != len(expected)
        or {row.get("id") for row in rows} != {row["id"] for row in expected}
        or any(result.get("error") for result in verification.get("results", []))
    ):
        return ["Jev: verification_incomplete"]
    second = confirmation_rows(verification)
    near_ids = {row["id"] for row in rows if near(row, threshold)}
    if second is None or not second.keys() <= near_ids:
        return [CONFIRMATION_MISMATCH]
    return [
        f"Jev: {row['id']}"
        for row in rows
        if not decided(row, second.get(row["id"]), threshold)
    ]


def weak(verification: dict[str, Any], threshold: float) -> list[str]:
    second = confirmation_rows(verification) or {}
    return [
        row["id"]
        for row in first_rows(verification)
        if not decided(row, second.get(row["id"]), threshold)
    ]
