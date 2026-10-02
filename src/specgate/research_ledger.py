"""Portable reads of the research ledger the host keeps in the run state; no gates."""

from typing import Any

from specgate.payload import CODE

# A cycle that stopped on the host or the provider judged no evidence.
OPERATIONAL = frozenset({"operational_unavailable", "topic_selection_required"})


def counted(state: dict[str, Any], question_id: str | None = None) -> list[dict[str, Any]]:
    """The attempts that spend a cycle and leave evidence behind.

    A cycle that stopped on the host or the provider is the error of the research,
    not a verdict on any source: it spends no cycle and no shared budget, and what
    it collected is no history, so the same sources may be collected again.
    """
    return [
        item
        for item in state.get("research_attempts", [])
        if item.get("reason") not in OPERATIONAL
        and (question_id is None or item["question_id"] == question_id)
    ]


def applicable_adjudication(
    state: dict[str, Any], gap_id: str, artifact_revision: str | None
) -> dict[str, Any] | None:
    """The judgment of a gap that recommends a revision of this very artifact.

    The host records it only after the evidence and the coverage cleared the gate of
    its authority; a revision other than the judged one gets no repair from it.
    """
    for row in state.get("flow_adjudications", []):
        if (
            row["gap_id"] == gap_id
            and row["status"] == "judged"
            and row["applicable"] is True
            and row["artifact_revision"] == artifact_revision
        ):
            result: dict[str, Any] = row
            return result
    return None


def stop(state: dict[str, Any], question_id: str) -> str | None:
    """The recorded stop that closes the research of a question.

    An operational stop is only the result of its cycle: the next reservation of
    the question researches again.
    """
    reason: str | None = state.get("research_stopped", {}).get(question_id)
    return None if reason in OPERATIONAL else reason


def exhausted(state: dict[str, Any], question_id: str, budget: int, *, limit: int = 3) -> str | None:
    if len(counted(state, question_id)) >= limit:
        return "research_limit"
    if len(counted(state)) >= budget:
        return "research_budget_exhausted"
    return None


def resumable(state: dict[str, Any], cycle_key: str) -> bool:
    """Whether the reservation stopped on an overflow report, so it may collect again.

    A bare ``collecting`` reservation is an unknown outcome and is never collected
    twice; an overflow report is the host's record that nothing was judged.
    """
    pending = state.get("research_pending")
    attempts = state.get("research_attempts") or [{}]
    return bool(
        pending
        and pending["phase"] == "collecting"
        and pending["cycle_key"] == cycle_key
        and attempts[-1].get("cycle_key") == cycle_key
        and attempts[-1].get("reason") == CODE
    )


def limits(state: dict[str, Any], claim_id: str) -> tuple[int, int]:
    """The attempt limit and the shared budget of the ticket research of a claim."""
    grants = state.get("ticket_research_continuations", [])
    extra = sum(
        row["authorization"]["additional_cycles"]
        for row in grants
        if row["claim_id"] == claim_id
    )
    base = state.get("workflow_policy", {}).get("research_budget", 12)
    supplemental = counted(state)[base:]
    spent = sum(row["question_id"] == f"tickets:{claim_id}" for row in supplemental)
    return (
        3 + extra,
        base + len(supplemental) + max(0, extra - spent),
    )


def admitted_texts(state: dict[str, Any]) -> dict[str, str]:
    """The text of every source a cycle selected, by the revision that names it."""
    return {
        item["revision"]: item["text"]
        for attempt in state.get("research_attempts", [])
        for item in attempt.get("selected", [])
    }
