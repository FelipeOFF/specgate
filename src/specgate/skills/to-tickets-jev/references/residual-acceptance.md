# Residual acceptance

When the automatic cycles of a claim have ended (its attempts, the shared budget
or stagnation, with or without a calibration manifest; never an operational stop, an
invalid or non-applicable manifest, a contradicting source or a cycle still reserved), the maintainer may
accept a graph whose remaining gaps are
verified claims below the threshold. `research_tickets` moves on to the next weak
claim whose cycles have not ended, so a graph with several residual claims
exhausts each one before the acceptance.
The `tickets_review` authorization carries `residual`, a list of `claim_id`,
`verdict` and `reason`. The host decides from the stored rows and the research
ledger, not from the list: each claim must be a current gap, verified on every
evaluation, already decided by the symmetric confirmation when it is near the
cutoff, and out of cycles. It refuses, and stores nothing, for a contradicted or
unsupported claim, a claim a research source contradicted at any attempt (even if
an extension erased the stop), a structural gap, a duplicate or a delegated
origin; only a human origin accepts. Review and `tickets_publication_begin` accept the graph when
every remaining gap is accepted (`verification_gaps` keeps the raw gaps), recompute
the acceptance on every read, and record under `residual_acceptance` the exact
graph revision, each claim's evaluations, mean, verdict, reason and how its cycles
ended; each publication entry keeps that record. A new graph revision starts with an empty list.
Before the publication starts, an authorized continuation (`tickets_research_extend`) reopens the
cycles and ends the acceptance: review again without the residual to resume the research. An
acceptance recorded when a host with no manifest counted the cycles as ended also ends there. Once the
publication starts the acceptance holds, whatever the host's gates become. Never lower the threshold
or accept a residual on a delegation.

## Wording residual

A `wording_check_review` gap whose recorded probabilities have none above 0.80 names one
action, the maintainer's `accept_wording_residual`. The person accepts it in the same
`residual` list, under the id `wording:<ticket>:ac-N`, the verdict `review` and a reason;
the acceptance keeps the probabilities the check recorded, and once it holds the criterion
leaves `next_actions`. The host refuses, and stores nothing, a wording gap with a probability
above 0.80 (`residual_wording_defect`: reformulate or split the criterion instead) and a
check with no probabilities, such as a transport error (`residual_wording_unscored`: verify
the graph again through the client). Only a human origin accepts it, as with the claims.
