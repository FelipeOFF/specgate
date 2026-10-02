---
name: implement-spec-jev
description: Implement the unlocked frontier of an approved ticket graph across authorized repositories, ending each ticket at validated draft PRs. Use after to-tickets-jev publishes a reviewed graph.
---

# Implement Spec with Jev

Load the approved spec revision and ticket graph. Require a delivery manifest
with one `delivery_id`, explicit `owner/repository` entries, local repository
roots, base branches, and deterministic checks. Reject duplicate repositories,
duplicate roots, undeclared repositories, credentials, session references, and
secrets in the manifest.

Observe every published ticket before selecting work. Implement only tickets in
the current unlocked frontier. Each ticket belongs to exactly one repository;
cross-repository work remains connected by graph dependencies rather than a
shared branch or commit.

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

Stop at draft pull requests. Do not merge, remove draft status, deploy, rewrite
published history, or bypass checks. Return authorization, missing adapters,
ambiguous publications, invalid worktrees, and destructive recovery choices to
a consolidated exception report. Reuse valid delegated authority for routine
actions and draft publication. Continue independent branches before asking the
human for the smallest missing decision; never change a technical gate to avoid
that decision.
