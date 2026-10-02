"""Judge ticket acceptance criteria against worktree evidence before a draft PR."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from specgate.shared.domain.inputs import Item, checked_text

Action = Literal["publish", "needs_human", "failed"]
Code = Literal[
    "verification_passed",
    "verification_unsupported",
    "verification_contradicted",
    "verification_operational",
    "verification_failed",
]

_OPERATIONAL = (
    r"reposit[oó]rio p[uú]blico nasce",
    r"hist[oó]rico novo",
    r"releases? npm",
    r"trusted publishing",
    r"instala pelo pipx",
    r"instala pelo npx",
    r"calibra[cç][aã]o real",
    r"or[cç]amento de chamadas",
    r"secret do provider",
    r"chamadas pagas",
)


@dataclass(frozen=True)
class VerificationDecision:
    action: Action
    code: Code
    auto_advance: bool = False


def ticket_claims(ticket: dict[str, Any]) -> list[Item]:
    ticket_id = str(ticket["id"])
    criteria = ticket.get("acceptance_criteria") or []
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("O ticket precisa de critérios de aceite.")
    return [
        Item(id=f"{ticket_id}:ac-{index}", text=str(text))
        for index, text in enumerate(criteria, 1)
    ]


def is_operational_claim(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _OPERATIONAL)


def pack_evidence(
    worktree: Path,
    checks: list[dict[str, Any]],
    ticket: dict[str, Any],
) -> str:
    files = _git(worktree, "ls-files").splitlines()
    chunks = [
        f"ticket: {ticket.get('id', '')}",
        f"behavior: {ticket.get('behavior', '')}",
        "checks:",
        *[
            f"- {item.get('name')}: {'passed' if item.get('passed') else 'failed'}"
            for item in checks
        ],
        "files:",
    ]
    for relative in files:
        path = worktree / relative
        if not path.is_file() or path.stat().st_size > 16_000:
            chunks.append(f"- {relative}")
            continue
        chunks.append(f"- {relative}\n{path.read_text(errors='replace')}")
    return checked_text("\n".join(chunks))


def interpret_verification(
    verdicts: list[dict[str, Any]],
    claims: list[Item],
) -> VerificationDecision:
    by_id = {claim.id: claim for claim in claims}
    if {row.get("id") for row in verdicts} != set(by_id):
        return VerificationDecision(action="needs_human", code="verification_failed")
    code_rows = []
    operational_rows = []
    for row in verdicts:
        claim = by_id[str(row.get("id"))]
        if is_operational_claim(claim.text):
            operational_rows.append(row)
        else:
            code_rows.append(row)
    if any(row.get("verdict") == "contradicted" for row in code_rows):
        return VerificationDecision(action="failed", code="verification_contradicted")
    if any(row.get("verdict") != "verified" for row in code_rows):
        return VerificationDecision(
            action="needs_human", code="verification_unsupported"
        )
    if operational_rows:
        return VerificationDecision(
            action="needs_human", code="verification_operational"
        )
    if not code_rows:
        return VerificationDecision(
            action="needs_human", code="verification_operational"
        )
    return VerificationDecision(action="publish", code="verification_passed")


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
