---
name: to-spec-jev
description: Turn a completed grill into a researched, Jev-verified specification and present one consolidated human review before publication. Use after grill-with-jev resolves the decision frontier.
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
verdict and its evidence revision. Contradicted or unsupported claims become
visible spec gaps; do not publish until the next revision resolves them.

Present one consolidated review in the harness:

- Approval records the reviewed artifact revision and the explicit authorization
  required by the configured tracker, then publication may proceed.
- Rejection calls `spec_reject` with the artifact revision and reason. Continue
  with `grill-with-jev` to clarify scope; do not publish the rejected artifact.

Close that review with a mermaid flowchart the person can read. Show each
active decision and the behavior it produces. Node labels are short titles in
the review's language. Keep the artifact revision inside the approval tool
call. The message ends on the diagram.

Publication uses only the tracker and credentials already authorized in the
harness. Reuse the same idempotency key while reconciling an uncertain result.
Never create a second artifact or tracker item for the same intent and revision.
