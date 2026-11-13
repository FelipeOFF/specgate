# Residual acceptance

The consolidated review may record a human residual. When the automatic cycles
of a claim have ended (its attempts, the shared budget or stagnation; never an
operational stop, a contradicting source or a cycle still reserved),
the maintainer may accept a spec whose remaining gaps are claims still `verified`
below the threshold. `SpecClient.research_spec` moves on to the next weak claim
whose cycles have not ended, so a spec with several residual claims exhausts each
one before the acceptance. The `spec_review` authorization carries `residual`, a list
of `claim_id` (`fidelity:<id>` or `coverage:<id>`), `verdict` and `reason`. The host
decides from the stored rows and the research ledger, not from the list: each claim
must be a current gap, verified, and out of cycles. It refuses, and stores nothing,
for a contradicted or unsupported claim, a claim a research source contradicted, a
structural gap, a duplicate or a delegated origin; only a human origin accepts, so
`SpecClient.review_delegated` never does. The threshold and the support criterion do
not change: `gaps` keeps listing the claim, and the spec is `ready` only when an
acceptance that still holds covers every gap. The host records under
`residual_acceptance` the exact spec revision, each claim's evaluation, verdict,
reason and how its cycles ended; the publication intent keeps that record. A new spec
revision starts with an empty list, and a research cycle that reopens before the
publication starts ends the acceptance. Once the publication starts it holds.

## Wording residual

A `wording_check_review` gap whose five recorded probabilities have none above 0.80 names
one action: the `next_actions` of `fidelity:decision-<slug>` are
`["accept_wording_residual"]`, the maintainer's. The person accepts it in the same
`residual` list, under the id `wording:decision-<slug>`, the verdict `review` and a
reason; the acceptance keeps the probabilities the check recorded. Until then the research
ends in `wording_residual` (to `human_review` even with a delegation); once that acceptance
holds, the claim takes the actions of its evidence. The host refuses, and stores nothing, a
wording gap with a probability above 0.80 (`residual_wording_defect`: reformulate or split
the decision instead) and a check that kept no probabilities, such as an error or the mock
(`residual_wording_unscored`): only a new verification resolves it, through the client.
Only a human origin accepts it, as with the claims.

The host keeps a decision whose check did not pass the gate as the gap `wording_check_review`.
When the `fidelity:decision-<slug>` claim of that decision is also weak and
names no action, the research ends in `no_action_available` (to `human_review` even with a
delegation); when it is strong, the gap stays on the spec, the research ends with no handoff
and the spec stays in review.
