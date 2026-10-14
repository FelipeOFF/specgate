"""The lineage of a draft, resolved against the revisions stored before it.

The agent declares the lineage of a part in the revision that divides or reformulates the
decision (or the criterion). The field is optional and empty by default, so a later revision
that rebuilds the draft can leave it out, and the check it owes must not leave with it. A part
that keeps its id and its text from the previous revision keeps its lineage too, until the
equivalence check against its original was accepted: the entry is inherited, the check is
required again and the reader still sees it. A part that is edited, or whose division was
accepted, inherits nothing; an edited part declares its lineage again.

The same walk resolves every stored revision first, oldest to newest, so the lineage a later
revision declares or inherits is read against the effective lineage of the ones before it.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from specgate.claim_equivalence import judge, lineage_view
from specgate.decision_lineage import Group, Revision, groups_of
from specgate.verification_refs import digest
from specgate.wording import AgentRefusal

# The finding that tells the agent which parts of a refused lineage it did not declare.
INHERITED = "lineage_inherited"

TextsOf = Callable[[Any], dict[str, str]]
Step = tuple[Revision, "Resolution"]


@dataclass(frozen=True)
class Resolution:
    """What the lineage of a draft comes to."""

    # The entries that bind the draft: the ones it declares and the ones it inherits.
    lineage: Mapping[str, Mapping[str, Any]]
    # The originals and parts to check; empty when the lineage cannot be resolved.
    groups: list[Group]
    # What a reader sees from each part: the effective lineage and any division already accepted.
    view: dict[str, dict[str, Any]]
    # The parts, with the digest of their text, whose equivalence check was accepted.
    settled: frozenset[tuple[str, str]]
    # Whether the lineage could not be resolved (only a lenient resolution gets here).
    failed: bool = False


def _inherited(
    previous: Step | None, texts: Mapping[str, str], declared: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    """The entries of the previous revision that follow a part which did not change."""
    if previous is None:
        return {}
    revision, resolution = previous
    return {
        part: revision.lineage[part]
        for part, text in texts.items()
        if part not in declared
        and part in revision.lineage
        and revision.texts.get(part) == text
        and (part, digest(text)) not in resolution.settled
    }


def _view(
    lineage: Mapping[str, Any],
    groups: Sequence[Group],
    record: Any,
    previous: Step | None,
    texts: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """The lineage of every part a reader can follow, accepted divisions included."""
    view = lineage_view(lineage, groups, record) if lineage else {}
    if previous is None:
        return view
    revision, resolution = previous
    carried = [
        part
        for part, text in texts.items()
        if part not in lineage
        and part in resolution.view
        and revision.texts.get(part) == text
    ]
    for part in carried:
        view[part] = dict(resolution.view[part])
    if carried:
        for part, row in view.items():
            row["siblings"] = [
                other
                for other, found in view.items()
                if other != part and found["original"] == row["original"]
            ]
    return view


def _resolve(
    draft: Mapping[str, Any],
    texts: dict[str, str],
    field: str,
    steps: Sequence[Step],
    record: Any,
    *,
    strict: bool,
) -> Resolution:
    written = draft.get(field)
    declared: dict[str, Any] = written if isinstance(written, dict) else {}
    previous = steps[0] if steps else None
    inherited = _inherited(previous, texts, declared)
    lineage = {**inherited, **declared}
    groups: list[Group] = []
    failed = False
    if lineage:
        try:
            groups = groups_of(lineage, texts, [revision for revision, _ in steps])
        except AgentRefusal as refused:
            if not strict:
                failed = True
            elif inherited:
                raise AgentRefusal(
                    refused.code,
                    str(refused),
                    [*refused.findings, {"problem": INHERITED, "parts": list(inherited)}],
                ) from None
            else:
                raise
    settled = frozenset(
        (part, digest(text))
        for group in groups
        if judge(record, group)[0] == "accepted"
        for part, text in group.parts
    )
    return Resolution(
        lineage, groups, _view(lineage, groups, record, previous, texts), settled, failed
    )


def _recorded(artifact: Mapping[str, Any]) -> Any:
    verification = artifact.get("verification")
    return verification.get("equivalence") if isinstance(verification, dict) else None


def resolve(
    draft: Mapping[str, Any],
    earlier: Sequence[Mapping[str, Any]],
    texts_of: TextsOf,
    field: str,
    record: Any = None,
    *,
    strict: bool = True,
) -> Resolution:
    """Resolve the lineage `field` of `draft` against the stored revisions before it.

    `earlier` are those revisions, newest first, each with its `draft` and `verification`;
    `texts_of` gives the texts of a draft by id; `record` is the equivalence the draft's own
    verification stored, for the reader's view. A strict resolution raises the typed refusal
    of an unresolvable lineage (naming the parts it inherited, if any); a lenient one returns
    it with no groups and `failed` set, which is what a stored revision or a read may carry.
    """
    steps: list[Step] = []
    for artifact in reversed(earlier):
        stored = artifact.get("draft") if isinstance(artifact, dict) else None
        if not isinstance(stored, dict):
            continue
        texts = texts_of(stored)
        resolution = _resolve(stored, texts, field, steps, _recorded(artifact), strict=False)
        steps.insert(0, (Revision(texts, resolution.lineage), resolution))
    return _resolve(draft, texts_of(draft), field, steps, record, strict=strict)
