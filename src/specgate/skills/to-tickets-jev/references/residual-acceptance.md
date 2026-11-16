# Residual acceptance

When the automatic cycles of a claim have ended (its attempts, the shared budget,
stagnation, or an `evidence_gate_closed` where the Jev judged the new source and its gate
refused it, with or without a calibration manifest; never an operational stop, an invalid
or non-applicable manifest, a contradicting source, a cycle still reserved, or an
`evidence_gate_closed` whose Jev gates passed and whose binding failed, as between a
client and a host of different versions), the maintainer may
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
the graph again through the client). `TicketClient.recheck_wording` makes that verification from
the stored graph: it asks again only the checks with no probabilities, reuses every verdict and
every other check, and the host counts no rejection for it, even after the third. Only a human origin accepts it, as with the claims.

## Answers of the renota

`TicketClient.recheck_wording` stores nothing in three answers:

- `wording_check_unscored`: no check it asked again came back with a score. If the transport
  failed, run it again once the transport is back; if the host answered with no score (the
  mock, an error of the host or of the provider), the same answer comes back while that cause
  lasts: stop at the dead end, and the person decides how the graph goes on.
- `wording_recheck_unavailable`: it sent nothing, because there is no stored graph, no check of
  it left without a score, or a research cycle pending. Finish a pending cycle through
  `TicketClient.research_tickets` with its key, then run the renota again if the gap is still there;
  otherwise read `tickets_get` again and follow the `review_exits` of each blocking gap.
- `research_changed`: it sent nothing, because a source the stored verification read (a project
  file, or the admitted research) changed or is gone, so its verdicts no longer describe what Jev
  would read. Put back a project file changed by mistake and run the renota again; otherwise
  verify the graph again through `TicketClient.synthesize` (or `specgate flow`). That is a new
  evaluation: it counts a rejection when it leaves a claim weak, and after the third rejection
  the person decides.
