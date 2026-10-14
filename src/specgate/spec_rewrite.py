"""The rewrite of decisions of an approved spec, under the standing grant of the operation.

The grant is the person's and lives on their machine, so the client reads it and checks it
again right before the call that writes (`rewrite_authority_valid`), and the call carries the
revision of the grant it used. What the host sees is the draft, the stored revisions and the
verification, and it requires of a rewrite what a common revision does not:

- the spec on the run is approved (`ready` or `published`), and the draft changes nothing but
  its implementation decisions, each of which that changes declares its lineage (a division
  or a reformulation of an approved decision) and carries no approval of its own;
- every original of the lineage is a decision of the approved revision, read as that revision
  holds it, and its equivalence check passed the gate with no defect. A defect refuses the parts
  as `equivalence_rejected` (the agent revises); a check the gate did not pass, or a client that
  skipped it, refuses the rewrite as `rewrite_equivalence_unverified`, with nothing stored in
  either case;
- no implementation has its draft pull request claimed: the worker that claimed it opens the
  pull request without reading the run again, so the rewrite waits for that publication.

A refused rewrite stores no revision. An accepted one stores the revision as a spec in review,
with the record of the rewrite, so the ticket graph of the approved revision is stale and the
implementation waits for the revision to be approved. The draft pull request of a delivery
shows the records behind the revision it rests on (`rewrite_audit`), next to the decisions
they replaced.
"""

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from specgate.claim_equivalence import defective, judge
from specgate.decision_lineage import Group, Revision, groups_of
from specgate.grill_contracts import GrillError
from specgate.slices.equivalence import DEFECTS
from specgate.spec_claims import decision_texts, earlier_specs
from specgate.wording import EquivalenceRejected, RewriteOutOfScope

# The client refuses a rewrite whose grant is gone, expired or without the operation.
REWRITE_NOT_AUTHORIZED = "rewrite_not_authorized"
REWRITE_NOT_APPROVED = "rewrite_spec_not_approved"
REWRITE_UNVERIFIED = "rewrite_equivalence_unverified"
IMPLEMENTATION_PUBLISHING = "implementation_publishing"
APPROVED = frozenset({"ready", "published"})
# The sections of a draft a rewrite changes; every other one stays as the approved revision has it.
REWRITABLE = frozenset({"implementation_decisions", "decision_lineage"})


class SpecRewrite(BaseModel):
    """The grant a rewrite of approved decisions was made under, as the client checked it."""

    model_config = ConfigDict(extra="forbid", strict=True)
    delegation_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    project: str = Field(min_length=1, max_length=4096)
    repositories: list[str] = Field(default_factory=list, max_length=50)
    reference: str = Field(min_length=1, max_length=1000)


PUBLISHING_MESSAGE = (
    "Uma implementação já reivindicou a publicação do draft PR e vai abri-lo sem reler a run; "
    "reconcilie essa publicação antes de reescrever a spec."
)


def publication_claimed(state: dict[str, Any]) -> bool:
    """Whether a worker holds the claim of a draft pull request it has not recorded yet."""
    implementations = state.get("implementations")
    return isinstance(implementations, dict) and any(
        isinstance(item, dict) and item.get("status") == "publishing"
        for item in implementations.values()
    )


def require_approved(state: dict[str, Any], rewrite: SpecRewrite) -> dict[str, Any]:
    """The approved spec on the run, or the refusal: a spec in review is revised the usual way.

    A published spec lives in a repository, and the grant the client names must cover it, as
    the client checked: the host does not read the grant, but it does not store a rewrite
    whose own claim leaves the repository out. A draft pull request already claimed by a worker
    is opened without the run being read again, so the rewrite waits until it is reconciled.
    """
    spec = state.get("spec")
    if not isinstance(spec, dict) or spec.get("status") not in APPROVED:
        raise GrillError(
            REWRITE_NOT_APPROVED,
            "Só uma spec aprovada admite a reescrita delegada de decisão; "
            "uma spec em revisão volta pela síntese comum.",
        )
    if publication_claimed(state):
        raise GrillError(IMPLEMENTATION_PUBLISHING, PUBLISHING_MESSAGE)
    published = state.get("publication") or {}
    repository = published.get("repository")
    if (
        spec.get("status") == "published"
        and isinstance(repository, str)
        and repository.casefold() not in {name.casefold() for name in rewrite.repositories}
    ):
        raise GrillError(
            REWRITE_NOT_AUTHORIZED,
            "A concessão da reescrita não cobre o repositório em que a spec foi publicada.",
        )
    return spec


def rewrite_groups(approved: dict[str, Any], proposed: dict[str, Any]) -> list[Group]:
    """The originals a rewrite replaces and their parts, read against the approved revision alone.

    The original of a rewrite is the decision as the approved revision holds it, whatever the
    older revisions said and whatever lineage the approved revision itself declares: an entry
    that repeats one the approved revision carries still resolves to the approved text. An entry
    on a part the rewrite left as it was replaces nothing now, so it is left out; the agent
    that keeps the lineage it received is not refused for it. Raises the typed refusal of
    `decision_lineage` when an entry of a changed part cannot be resolved.
    """
    old, new = decision_texts(approved.get("draft")), decision_texts(proposed)
    written = proposed.get("decision_lineage")
    declared = written if isinstance(written, dict) else {}
    entries = {
        part: entry
        for part, entry in declared.items()
        if not (part in new and old.get(part) == new[part])
    }
    return groups_of(entries, new, [Revision(old, {})]) if entries else []


# A revision these statuses name stands for nothing any more: its decisions are not a reference.
GONE = frozenset({"stale", "rejected"})


def established(revision: Any) -> bool:
    """Whether the decisions of a stored revision were settled by a person or by the operation.

    A person approved it (its status is ready or published, or its seams stay approved after a
    change of tracker sent it back to review), or the rewrite operation wrote it with the
    equivalence of each decision accepted. A revision that was stale or rejected settles nothing.
    """
    if not isinstance(revision, dict) or revision.get("status") in GONE:
        return False
    authorization = revision.get("authorization")
    return bool(
        revision.get("status") in APPROVED
        or revision.get("rewrite")
        or (isinstance(authorization, dict) and authorization.get("seams_approved") is True)
    )


def replaced_approved(state: dict[str, Any]) -> bool:
    """Whether the spec on the run replaced a decision of the newest revision that was settled.

    The rewrite operation is the one way a delegation does that, and it leaves its record on
    the revision. The reference is the newest revision, the spec on the run first, that a person
    approved or the operation wrote, so it holds after a change of tracker sent an approved
    revision back to review, and after a synthesis that kept what the operation wrote. A
    revision that changed a decision of the reference without being one was written by a common
    synthesis, and only a person approves it.
    """
    spec = state.get("spec")
    reference = next((item for item in earlier_specs(state) if established(item)), None)
    if not isinstance(spec, dict) or reference is None:
        return False
    new = decision_texts(spec.get("draft"))
    return any(
        new.get(identifier) != text
        for identifier, text in decision_texts(reference.get("draft")).items()
    )


def kept_record(previous: Any, proposed: dict[str, Any]) -> dict[str, Any] | None:
    """The record of the rewrite that wrote the revision on the run, while a synthesis keeps it.

    A later synthesis, such as the one that ends a research cycle, that leaves every decision as
    the rewritten revision has it says the same about them, so the record goes with it. One that
    changes, drops or adds a decision does not: the record stays with the revision that wrote it,
    in the history.
    """
    if not isinstance(previous, dict) or previous.get("status") in GONE:
        return None
    record: dict[str, Any] | None = previous.get("rewrite")
    if record and decision_texts(previous.get("draft")) == decision_texts(proposed):
        return deepcopy(record)
    return None


def human_approved(revision: Any) -> bool:
    """Whether a person approved the revision, as opposed to the delegation or nobody.

    It is approved (`ready` or `published`), or its seams stay approved after a change of
    tracker sent it back to review, and the approval is not the delegated review's
    (`origin` `automated`). A revision that was stale or rejected settles nothing.
    """
    if not isinstance(revision, dict) or revision.get("status") in GONE:
        return False
    authorization = revision.get("authorization")
    stored = authorization if isinstance(authorization, dict) else {}
    approved = revision.get("status") in APPROVED or stored.get("seams_approved") is True
    return bool(approved and stored.get("origin", "human") == "human")


def rewrite_audit(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The delegated rewrites behind the revision of the spec the delivery rests on, oldest first.

    The audit of the pull request reads the record `spec.rewrite` of each revision, the one on
    the run first and then the history, back to the last revision a person approved that has
    no record of its own: what a person approved without a rewrite is settled, and the
    rewrites before it are not this delivery's. A revision that carries a record is never the
    stop, even when a person approved it, so the rewrite stays visible next to the approval.
    A revision with no record that nobody approved, or that the delegation approved, is crossed:
    a common synthesis can store a revision with no record (it added a decision, say) after a
    revision that had one, and the rewrite it follows does not disappear from the audit. A
    revision that was stale or rejected settles nothing and is skipped.

    A record that a later synthesis kept (the same decisions) is on both revisions and appears
    once, at the revision that wrote it. Each entry is the record plus that `revision`. Derived
    on every read from the stored revisions, never stored.
    """
    written: list[tuple[Any, dict[str, Any]]] = []
    for revision in earlier_specs(state):
        if not isinstance(revision, dict) or revision.get("status") in GONE:
            continue
        record = revision.get("rewrite")
        if isinstance(record, dict):
            written.append((revision.get("revision"), record))
        elif human_approved(revision):
            break
    audit: list[dict[str, Any]] = []
    seen: list[dict[str, Any]] = []
    for identifier, record in reversed(written):
        if record not in seen:
            seen.append(record)
            audit.append({"revision": identifier, **deepcopy(record)})
    return audit


def _problems(
    approved: dict[str, Any],
    proposed: dict[str, Any],
    groups: Sequence[Group],
    approval_given: bool,
) -> list[dict[str, Any]]:
    """What the draft changes beyond the decisions the lineage accounts for."""
    problems: list[dict[str, Any]] = []
    if approval_given:
        problems.append({"problem": "approval_in_rewrite"})
    before = approved.get("draft") or {}
    sections = sorted(
        key
        for key in {*before, *proposed} - REWRITABLE
        if proposed.get(key) != before.get(key)
    )
    if sections:
        problems.append({"problem": "sections_changed", "sections": sections})
    old, new = decision_texts(before), decision_texts(proposed)
    originals = {group.original for group in groups}
    parts = {part for group in groups for part, _ in group.parts}
    if not groups:
        problems.append({"problem": "no_decision_rewritten"})
    # The original is the decision as the approved revision holds it, not a text of an older one.
    if unknown := sorted(
        group.original for group in groups if old.get(group.original) != group.original_text
    ):
        problems.append({"problem": "original_not_approved", "originals": unknown})
    # A decision that left, changed or came with no lineage was not checked against anything.
    changed = {identifier for identifier, text in old.items() if new.get(identifier) != text}
    added = set(new) - set(old) - parts
    if unaccounted := sorted((changed - originals) | added):
        problems.append({"problem": "decision_unaccounted", "decisions": unaccounted})
    return problems


def check_scope(
    approved: dict[str, Any],
    proposed: dict[str, Any],
    groups: Sequence[Group],
    *,
    approval_given: bool = False,
) -> None:
    """Refuse a draft that changes more than the decisions its lineage accounts for.

    The client runs it before it asks the Jev anything, and the host runs it again.
    """
    if problems := _problems(approved, proposed, groups, approval_given):
        raise RewriteOutOfScope(problems)


def rewrite_record(
    approved: dict[str, Any],
    proposed: dict[str, Any],
    groups: Sequence[Group],
    verification: dict[str, Any],
    rewrite: SpecRewrite,
    *,
    approval_given: bool,
) -> dict[str, Any]:
    """The record of an accepted rewrite, or the typed refusal that stores nothing.

    It keeps what the audit of the pull request reads: the original decisions and the
    text that replaces them, what the equivalence check found, and the revision of the grant.
    """
    check_scope(approved, proposed, groups, approval_given=approval_given)
    equivalence: Any = verification.get("equivalence")
    if findings := defective(equivalence, groups):
        raise EquivalenceRejected(findings)
    unverified = [
        group.original for group in groups if judge(equivalence, group)[0] != "accepted"
    ]
    if unverified:
        raise GrillError(
            REWRITE_UNVERIFIED,
            "A equivalência de "
            + ", ".join(unverified)
            + " não passou pelo gate; a reescrita delegada não vale sem ela.",
        )
    checks = equivalence["checks"]
    decisions = []
    for group in groups:
        entry = checks[group.original]
        decisions.append(
            {
                "original": group.original,
                "kind": group.kind,
                "original_text": group.original_text,
                "parts": [{"id": part, "text": text} for part, text in group.parts],
                "equivalence": {
                    "state": "accepted",
                    "probabilities": dict(zip(DEFECTS, entry["probabilities"], strict=True)),
                    "authority": equivalence["authorities"][entry["authority"]],
                },
            }
        )
    return {
        "replaces": approved["revision"],
        "delegation": {
            "revision": rewrite.delegation_revision,
            "reference": rewrite.reference,
            "project": rewrite.project,
            "repositories": rewrite.repositories,
        },
        "decisions": decisions,
    }
