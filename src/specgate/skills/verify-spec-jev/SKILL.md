---
name: verify-spec-jev
description: Judge a ticket's acceptance criteria against worktree evidence through jev_verify before opening a draft pull request. Use as the last gate of implement-spec-jev.
---

# Verify Spec with Jev

After the ticket's declared checks pass and before a draft pull request, treat
each acceptance criterion as a claim. Do not ask Jev to browse the repository.
Pack deterministic evidence from the worktree: tracked files, check results,
commit, and ticket behavior. Keep the packet inside the 64 KB evidence limit.

Call `jev_verify` with stable claim IDs `{ticket-id}:ac-{n}`. Classify claims
that require a public repository, registry publish, paid calibration, or a
provider secret as operational. Those stay in human review even when code
claims are supported.

Interpret the verdicts:

- Every code claim `verified` and no operational leftovers: the draft PR may
  be created once publication is authorized. Without a manifest the verdicts pass
  the confidence policy: `gate.basis=confidence_policy`, `calibrated=false`,
  confidence and support strictly above the run threshold, and a
  `gate.policy_binding` the client confirmed. `auto_advance=true` only recommends
  and the score is an uncertainty filter, not measured accuracy; the grant for the
  repository and `draft_pr` opens the PR. The person's explicit authorization of the
  drafts (`specgate flow --authorize-drafts`) opens it instead of the grant, and then
  the draft's verification skips this automatic predicate: `verified` code claims are
  enough. Only the person passes that flag, never the agent on its own.
- Any code claim `unsupported`: keep the ticket in review. Do not publish.
- Any code claim `contradicted`: fail the ticket. Do not publish.
- Operational claims only: `needs_human`. Do not treat them as a stall.

Do not close issues, merge, deploy, retry implementation, or terminate the
harness. Record the verification with the implementation. Mock, a result without
`gate.basis`, a binding the client did not confirm, and error results stay in
review, unless the person passed `--authorize-drafts` for the drafts.
