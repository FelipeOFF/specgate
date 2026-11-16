"""Judge ticket acceptance criteria against worktree evidence before a draft PR."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from specgate.payload import LIMIT
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
# A touched file travels whole up to this size; a larger one goes by name only.
FILE_LIMIT = 16_000
# The names left out take at most this many bytes; the others go by count.
NAMES_LIMIT = 4_000
_LEFT_OUT = "not included (above 16 KB or past the size limit):"


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


def evidence_limit(claims: list[Item], context: dict[str, Any] | None = None) -> int:
    """The bytes the evidence may take in the one text the host checks for `jev_verify`.

    The host serializes the claims, the evidence and the context into a text of at most
    `LIMIT` bytes; the evidence gets what the others leave, less a margin.
    """
    state = {
        "task": "verify",
        "claims": {claim.id: claim.text for claim in claims},
        "evidence": "",
        **({"context": context} if context is not None else {}),
    }
    return LIMIT - _size(json.dumps(state, ensure_ascii=False)) - 1024


def pack_evidence(
    worktree: Path,
    checks: list[dict[str, Any]],
    ticket: dict[str, Any],
    base: str | None = None,
    *,
    limit: int = LIMIT,
) -> str:
    """What the ticket committed, never the whole repository.

    The diff from the merge base with `base` (its remote branch, else the local one) to the
    commit, then each file it touches as the commit has it, up to 16 KB each. Without a base
    that resolves, the last commit stands for the change. What would take the text, as JSON,
    past `limit` bytes goes by name only, and past `NAMES_LIMIT` bytes of names, by count.
    """
    start, origin = _start(worktree, base)
    span = ("-r", "--no-renames", start, "HEAD")
    # Per name, `:old_mode new_mode old_object new_object status`.
    listed = _git(worktree, "diff-tree", "-z", *span).split("\0")
    changes = dict(zip(listed[1::2], (meta.split() for meta in listed[::2])))
    names = list(changes)
    head = [
        f"ticket: {ticket.get('id', '')}",
        f"behavior: {ticket.get('behavior', '')}",
        "checks:",
        *[
            f"- {item.get('name')}: {'passed' if item.get('passed') else 'failed'}"
            for item in checks
        ],
        f"diff ({origin}):",
    ]
    # Room for the names left out is kept before any diff or content.
    reserve = [*head, "files:", _LEFT_OUT, f"- and {len(names)} more files"]
    named = min(sum(_cost(f"- {name}") for name in names), NAMES_LIMIT)
    room = limit - sum(map(_cost, reserve)) - named
    body: list[str] = []
    left: dict[str, None] = {}

    def add(name: str, chunk: str | None) -> None:
        nonlocal room
        if chunk is not None and (cost := _cost(chunk)) <= room:
            body.append(chunk)
            room -= cost
        else:
            left[name] = None

    # One run of git for the whole change; a file is read only when its size still fits.
    patches = _patches(_git(worktree, "diff-tree", "-p", *span))
    for name, diff in zip(names, patches, strict=True):
        add(name, diff)
    body.append("files:")
    # The commit's own text, never the disk's, and only a file (100644, 100755) has one: a
    # deleted file (000000), a link (120000) or a submodule (160000) is left to the diff.
    blobs = {name: meta[3] for name, meta in changes.items() if meta[1].startswith("100")}
    # The objects go on stdin, so no run of git grows with the change.
    stdin = "".join(f"{blob}\n" for blob in blobs.values())
    sizes = _git(worktree, "cat-file", "--batch-check=%(objectsize)", stdin=stdin).split()
    for (name, blob), size in zip(blobs.items(), map(int, sizes), strict=True):
        fits = size <= FILE_LIMIT and _cost(f"- {name}") + size <= room
        add(name, f"- {name}\n{_git(worktree, 'cat-file', 'blob', blob)}" if fits else None)
    return checked_text("\n".join([*head, *body, *_left_out(list(left))]))


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


def _size(text: str) -> int:
    return len(text.encode())


def _cost(line: str) -> int:
    """The bytes one line takes in the text as JSON, with the line break after it."""
    return _size(json.dumps(line, ensure_ascii=False))


def _patches(patch: str) -> list[str]:
    """The patch of each name, in order; a change of type comes as two patches of one name."""
    patches: list[str] = []
    for part in filter(None, re.split(r"^(?=diff --git )", patch, flags=re.MULTILINE)):
        if patches and part.partition("\n")[0] == patches[-1].partition("\n")[0]:
            patches[-1] += part
        else:
            patches.append(part)
    return patches


def _left_out(names: list[str]) -> list[str]:
    """The names left out while they take `NAMES_LIMIT` bytes, then how many others."""
    lines = [_LEFT_OUT] if names else []
    room = NAMES_LIMIT
    for index, name in enumerate(names):
        room -= _cost(f"- {name}")
        if room < 0:
            return [*lines, f"- and {len(names) - index} more files"]
        lines.append(f"- {name}")
    return lines


def _start(worktree: Path, base: str | None) -> tuple[str, str]:
    """The commit the change starts from, and where it came from."""
    for ref in (f"refs/remotes/origin/{base}", f"refs/heads/{base}") if base else ():
        if found := _resolve(worktree, "merge-base", "HEAD", ref):
            return found, f"since the merge base with {ref.split('/', 2)[2]}"
    # The parent of the commit, or the empty tree when it is the first one.
    start = _resolve(worktree, "rev-parse", "--verify", "--quiet", "HEAD^") or _git(
        worktree, "hash-object", "-t", "tree", "--stdin", stdin=""
    ).strip()
    return start, "last commit only: the base did not resolve"


def _resolve(worktree: Path, *args: str) -> str | None:
    try:
        return _git(worktree, *args).strip() or None
    except subprocess.CalledProcessError:
        return None


def _git(worktree: Path, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), "--literal-pathspecs", *args],
        check=True,
        capture_output=True,
        input=stdin,
        text=True,
        errors="replace",
    )
    return result.stdout
