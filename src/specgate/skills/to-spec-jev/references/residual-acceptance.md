# Residual acceptance

The consolidated review may record a human residual. When the automatic cycles
of a claim have ended (its attempts, the shared budget, stagnation, or an
`evidence_gate_closed` where the Jev judged the new source and its gate refused it;
never an operational stop, a contradicting source, a cycle still reserved, or an
`evidence_gate_closed` whose Jev gates passed and whose binding failed, as between a
client and a host of different versions),
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
`SpecClient.recheck_wording` makes it from the stored spec: it asks again only the checks with
no probabilities, reuses every verdict and every other check, and the host counts no
rejection for it, even after the third.
Only a human origin accepts it, as with the claims.

The host keeps a decision whose check did not pass the gate as the gap `wording_check_review`.
When the `fidelity:decision-<slug>` claim of that decision is also weak and
names no action, the research ends in `no_action_available` (to `human_review` even with a
delegation); when it is strong, the gap stays on the spec, the research ends with no handoff
and the spec stays in review.

## Answers of the renota

`SpecClient.recheck_wording` stores nothing in three answers:

- `wording_check_unscored`: no check it asked again came back with a score. If the transport
  failed, run it again once the transport is back; if the host answered with no score (the
  mock, an error of the host or of the provider), the same answer comes back while that cause
  lasts: stop at the dead end, and the person decides how the spec goes on.
- `wording_recheck_unavailable`: it sent nothing, because there is no stored spec, no check of
  it left without a score, or a research cycle pending. Finish a pending cycle through
  `SpecClient.research_spec` with its key, then run the renota again if the gap is still there;
  otherwise read `spec_get` again and follow the `review_exits` of each blocking gap.
- `research_changed`: it sent nothing, because a source the stored verification read (a project
  file, or the admitted research) changed or is gone, so its verdicts no longer describe what Jev
  would read. Put back a project file changed by mistake and run the renota again; otherwise
  verify the spec again through `SpecClient.synthesize` (or `specgate flow`). That is a new
  evaluation: it counts a rejection when it leaves a claim weak, and after the third rejection
  the person decides.
