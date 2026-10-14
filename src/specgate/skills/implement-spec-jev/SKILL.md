---
name: implement-spec-jev
description: Implement the unlocked frontier of an approved ticket graph across authorized repositories, ending each ticket at validated draft PRs. Use after to-tickets-jev publishes a reviewed graph.
---

# Implement Spec with Jev

Load the approved spec revision and ticket graph. Require a delivery manifest
with one `delivery_id`, explicit `owner/repository` entries, local repository
roots, base branches, and deterministic checks. Reject duplicate repositories,
duplicate roots, undeclared repositories, credentials, session references, and
secrets in the manifest. This manifest lists repositories and checks; it is not a
calibration manifest, which no step of this flow asks for.

Observe every published ticket before selecting work. The unlocked frontier holds
the tickets whose blockers are complete; a draft pull request never completes a
ticket. A ticket that waits only on open blockers may start before their merge,
once each blocker has a verified draft: a pull request, its current commit, the
current graph revision, passed checks and an authorization that still holds. Each
ticket belongs to exactly one repository. In the same repository its pull request
is based on the branch of that blocker's draft; a blocker from another repository
is consumed as an explicit reference (pull request, commit, revision) and its
branch is never stacked. The stack fails closed: only that path moves on by
itself. If a blocker's pull request changes commit or base, leaves draft, merges
by squash or rebase, or is abandoned, or if the stack has a shape this version
does not support, the dependents are held for review with a code of their own and
no effect. Never start a record again or rewrite history on your own, and
retarget a pull request only after the forge confirms its blocker merged; a
person decides everything else.

For every selected ticket:

1. Read the repository's own rules and the exact ticket context.
2. Create or reuse a dedicated worktree and a typed branch for that ticket.
3. Keep the branch, commits, checks, and pull request inside its declared
   repository.
4. Run every check declared for that repository against the resulting commit.
5. Run `verify-spec-jev` against the ticket's acceptance criteria and the
   worktree evidence. Do not open a draft pull request when a code claim is
   unsupported or contradicted, or when only operational claims remain.
6. When checks and verification pass and publication is authorized, create one
   draft pull request containing the ticket reference, `delivery_id`, commit,
   and stable implementation marker.
7. Reconcile an uncertain publication response before retrying. Never create a
   second pull request for the same implementation claim.

Record a failed check on its ticket and continue other independent tickets in
the frontier. Dependents remain blocked by the graph; do not discard valid work
from unrelated repositories.

Never rewrite or split a decision of the approved spec while implementing, and do
not change a criterion that reads as compound or ambiguous: send it back to
`to-tickets-jev`. Jev only judges; it writes no code, test, criterion or pull
request text. A decision is rewritten only through the delegated rewrite of
`to-spec-jev` (`SpecClient.rewrite_decisions`), under a delegation in force that
includes `rewrite_decisions`, which only the person grants with
`specgate setup --allow-rewrite`. That rewrite makes the ticket graph stale and
stops the implementation until a new graph is published. Each delegated rewrite
appears in the draft pull request, under `## Reescritas delegadas`, beside the
original decision; the body the implementation client builds carries it, so keep
it.

Stop at draft pull requests. Do not merge, remove draft status, deploy, rewrite
published history, or bypass checks. Return authorization, missing adapters,
ambiguous publications, invalid worktrees, and destructive recovery choices to
a consolidated exception report. Reuse valid delegated authority for routine
actions and draft publication. A passing verification only recommends: it carries
`gate.basis` (`confidence_policy` with `calibrated=false` when there is no manifest)
and `execution_authorized` stays `false`. The draft needs the current grant for that
repository and `draft_pr`, or the person's explicit authorization of the drafts,
never the score. Continue independent branches before asking the human for the
smallest missing decision; never change a technical gate to avoid that decision.

`specgate flow` runs these stages after grill, research, spec and tickets and ends
at the same draft pull requests, without a calibration manifest. It resumes from
the state the host keeps and never merges, deploys or force-pushes.
