---
name: to-spec-jev
description: Turn a completed grill into a researched, Jev-verified specification and reuse valid delegated publication authority. Use after grill-with-jev resolves the decision frontier.
---

# To Spec with Jev

Start from the completed grill record. Load the installed `to-spec` skill and
preserve its template and testing-seam guidance.

Before synthesis, research the remaining technical and domain claims from
authorized project sources. Record every source and content revision. Do not use
secrets, harness transcripts, or an artifact as evidence for itself. If no
additional source is available, keep the spec in human review as
`research_insufficient`.

Synthesize the spec from the original scope, active decisions, domain vocabulary,
and research packet. Preserve public decision IDs and do not invent answers for
unresolved business questions.

Use `jev_verify` to check the problem, solution, and implementation decisions
against the original scope, active grill decisions, and research. Preserve each
verdict and its evidence revision. Check coverage of the original scope and every
active grill decision separately. Every blocking claim needs confidence and
support strictly above the frozen workflow threshold, with a minimum of 0.80.
A strong average cannot compensate for a weak claim.

For a weak claim, reuse `research-with-jev` and `research-filter-jev`. Reserve the
attempt through `spec_research_begin` before collection, bound to the exact spec
revision and `fidelity:<id>` or `coverage:<id>`. Record the hash-addressed filtered
packet through `spec_research_record`; only admitted independent evidence permits
reevaluation. The public package ships `SpecClient.research_spec`, which takes a
research planner and an optional repair callback. Reevaluate weak or materially
changed claims, retaining strong unchanged verdicts and all earlier results.

Allow at most three additional cycles per claim within the run's shared research
budget. Changing the draft or idempotency key does not reset the budget. Stop on
no new evidence, conflicting sources, an unreadable calibration manifest or an
uncertain external result. A host with no manifest researches too: it admits new
evidence by the confidence policy, and an installed manifest by its own binding.
Resume an existing intent before attempting another collection.
Persistent gaps go to the delivery's bounded adjudication or exception report;
include alternatives, sources, scores and reasons without asking repeatedly.

A `payload_too_large` result (`error.code`, with `size` and `limit` in bytes) means the
request passed the host's 64 KB limit (or its 256 KB HTTP body), not that Jev or the
provider failed. Stop in review and show the code, the size and the limit; never retry
it unchanged, raise the limit or spend another research cycle on it. Shrink the request
(fewer or shorter sources) and resume the same idempotency key: a reservation already
made resumes without a new one. After admission it stays in evaluation; before it, the
client has already reported the size to the host (sizes and ids, never content), which
keeps the reservation in `collecting` for that same key to collect again. Never collect
again from a bare `collecting` reservation: without that report it is an unknown
outcome.

Keep the spec, source hashes, verdict history and testing seams in versionable
`docs/specs/` files inside the project. Collection and filter packets stay in
`docs/research/`. These records allow another harness to resume the same revision.
Contradicted, unsupported or below-threshold claims remain visible gaps and keep
publication in review.

Review the exact artifact once. Read the current setup delegation and explicit
instructions already given for this delivery. When they cover the project,
destination and publication, call MCP `spec_review` after fidelity and coverage
gates pass. Send the exact artifact revision and authorization with
`seams_approved=true`, `publication_authorized=true`, `origin=automated`, the
global `delegation_revision`, canonical `project`, approved `repositories` and
the separate authorization `reference`. The public client exposes
`specgate.delegation.publication_authority` and `verification_allows_review` to
prepare these fields and check gates; `SpecClient.review_delegated` runs the same
review through the public client.
Never manufacture a human acceptance or overwrite an existing human rejection.

A missing grant or persistent technical gap becomes one exception in the delivery
report. Include the blocked action, research already done, recommendation and
only the alternatives the human must choose between. Continue independent work.
In manual mode, present a consolidated review; an explicit rejection calls
`spec_reject` and returns to the unresolved scope. Do not publish a rejected spec.

Publication uses only the tracker and credentials already authorized in the
harness. Reuse the same idempotency key while reconciling an uncertain result.
Immediately before each tracker mutation, recheck the live global delegation,
its revision, expiry, project, host and destination with
`specgate.delegation.review_authority_valid`. A stale or revoked grant retains
the action. An old review never authorizes a different destination.
Never create a second artifact or tracker item for the same intent and revision.
